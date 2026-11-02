"""Lightweight health/availability gate — the only evaluation the platform
runs itself. Checks that the service is up, answers a
completion, and the output is not obviously broken; then warms it up, so the
benchmark that follows meets an engine that has served before.

The check runs on a background thread: start() returns at once and poll()
answers RUNNING until it is done. The gate can take minutes (a slow first
answer, retries, the warm-up), and the worker's tick, which advances every
campaign, must not wait for it."""

import json
import re
import threading
import time
import uuid
import zlib
from concurrent.futures import Future, ThreadPoolExecutor
from functools import cache
from pathlib import Path
from typing import Any

import httpx

from app.evaluation.base import EvalOutcome, EvalStatus, Evaluator

_PROBE_PROMPT = "Reply with exactly the word OK."
# Generous enough that a reasoning model can think AND answer.
_PROBE_MAX_TOKENS = 512
# The warm-up: short requests sent at once after the probe passes. An engine
# pays one-time costs on its first forwards (graph capture or replay set-up,
# kernel JIT), and an engine that is several behind one API — data-parallel
# ranks, each with its own first request — pays them once per rank. One probe
# warms one rank; the benchmark's first request (and LLMBench's preflight)
# could land on a cold one and time out. A burst wide enough to reach every
# rank takes those costs here instead, inside this gate's budget.
_WARMUP_REQUESTS = 8
_WARMUP_MAX_TOKENS = 16
# Transient faults (a dropped connection, a 502/503/504 from a front door that
# is still settling, a timeout) are retried within the budget, this many tries
# in all, the waits between them growing.
_ATTEMPTS = 3
_BACKOFF_SECONDS = (5.0, 15.0)
_TRANSIENT_STATUS = frozenset({502, 503, 504})
# An answer that compresses this many times over is a loop, not language
# ("_2.5.5.5.5…", "_2_2_2_2…"): an engine whose config breaks its numerics
# still generates tokens, at full speed, and a benchmark that counts tokens
# scores it well. English, code and reasoning compress about 2-4×.
_LOOP_MIN_CHARS = 200
_LOOP_RATIO = 8.0
# Checks running at once; more wait their turn (and read RUNNING meanwhile).
_CONCURRENT_CHECKS = 8
# A finished check nobody polled (its run was stopped meanwhile) is dropped
# after this long.
_UNCLAIMED_SECONDS = 3600.0


class _Transient(Exception):
    pass


# What a launch that fails the gate for its output (rather than for being
# down) is failed as: the config broke the model, and a policy should learn
# that, not retry it.
GARBAGE_OUTPUT = "garbage_output"

# The sanity check after the probe: short grade-school math questions (from
# GSM8K's test set, MIT, bundled in data/), asked at once. A working model gets
# nearly all of them; the pass mark is low on purpose, to catch an engine that
# no longer computes, not to grade the model. Cheap next to the replay it
# guards: a minute or two, once per launch.
_SANITY_DATA = Path(__file__).parent / "data" / "gsm8k_sanity.jsonl"
_SANITY_SUFFIX = "\nSolve it, then give the final answer on its own last line as: #### <number>"
_FINAL = re.compile(r"####\s*\$?\s*(-?[\d,]*\.?\d+)")
_NUMBER = re.compile(r"-?\d[\d,]*\.?\d*")


@cache
def sanity_questions() -> tuple[tuple[str, float], ...]:
    rows = [json.loads(line) for line in _SANITY_DATA.read_text().splitlines() if line.strip()]
    return tuple((r["question"], float(r["answer"])) for r in rows)


def final_number(content: str, reasoning: str = "") -> float | None:
    """The answer's final number: after '####' if there is one, else the last
    number in the answer, else the last in the reasoning (a reasoning model cut
    off at its budget still shows whether it can count)."""
    for text in (content, reasoning):
        found = _FINAL.findall(text) or _NUMBER.findall(text)
        if found:
            try:
                return float(found[-1].replace(",", "").rstrip("."))
            except ValueError:
                continue
    return None


def looping(text: str) -> bool:
    """Whether a long answer is one short pattern repeated."""
    data = text.encode()
    return len(data) >= _LOOP_MIN_CHARS and len(data) / len(zlib.compress(data)) >= _LOOP_RATIO


class HealthEvaluator(Evaluator):
    name = "health"

    def __init__(
        self,
        timeout_seconds: float = 60.0,
        sanity_questions: int = 0,
        sanity_pass_rate: float = 0.5,
        sanity_max_tokens: int = 4096,
    ):
        # The whole gate's budget: the probe, its retries, the warm-up and the
        # sanity questions.
        self.timeout_seconds = timeout_seconds
        # 0 = no sanity check (the probe and the warm-up only).
        self.sanity_count = sanity_questions
        self.sanity_pass_rate = sanity_pass_rate
        self.sanity_max_tokens = sanity_max_tokens
        self._pool = ThreadPoolExecutor(max_workers=_CONCURRENT_CHECKS,
                                        thread_name_prefix="health")
        self._lock = threading.Lock()
        # ref -> (the check, when it was started)
        self._checks: dict[str, tuple[Future[EvalOutcome], float]] = {}

    def start(self, endpoint_url: str, served_model_name: str, context: dict[str, Any]) -> str:
        ref = f"health:{uuid.uuid4().hex[:12]}:{endpoint_url}"
        future = self._pool.submit(self._check, endpoint_url, served_model_name)
        with self._lock:
            self._drop_unclaimed()
            self._checks[ref] = (future, time.monotonic())
        return ref

    def poll(self, external_ref: str) -> EvalOutcome:
        """RUNNING until the check is done; then its outcome, once (the ref
        is forgotten). A ref this process does not know (a worker restart
        lost it, or it was read already) is a failure to re-run start() on."""
        with self._lock:
            entry = self._checks.get(external_ref)
            if entry is None:
                return EvalOutcome(status=EvalStatus.FAILED, error="health outcome lost; re-run")
            future, _ = entry
            if not future.done():
                return EvalOutcome(status=EvalStatus.RUNNING)
            del self._checks[external_ref]
        try:
            return future.result()
        except Exception as exc:  # noqa: BLE001 — a bug in the check is its verdict
            return EvalOutcome(status=EvalStatus.FAILED, error=f"{type(exc).__name__}: {exc}")

    def cancel(self, external_ref: str) -> None:
        """Forget the check; one already running finishes on its own."""
        with self._lock:
            entry = self._checks.pop(external_ref, None)
        if entry is not None:
            entry[0].cancel()

    def _drop_unclaimed(self) -> None:
        cutoff = time.monotonic() - max(_UNCLAIMED_SECONDS, self.timeout_seconds * 2)
        for ref, (future, started) in list(self._checks.items()):
            if future.done() and started < cutoff:
                del self._checks[ref]

    # -- checks --------------------------------------------------------------

    def _post(self, url: str, payload: dict[str, Any], deadline: float) -> httpx.Response:
        """One chat completion, retried through transient faults until it
        answers or the budget is spent. A non-transient answer (200, or a
        4xx/500 that says something about the request) is returned as is."""
        last = ""
        for attempt in range(_ATTEMPTS):
            left = deadline - time.monotonic()
            if left <= 1:
                break
            try:
                response = httpx.post(url, json=payload, timeout=left)
                if response.status_code not in _TRANSIENT_STATUS:
                    return response
                last = f"{response.status_code}: {response.text[:200]}"
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = f"{type(exc).__name__}: {exc}"
            if attempt < _ATTEMPTS - 1:
                time.sleep(min(_BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)],
                               max(deadline - time.monotonic() - 1, 0)))
        raise _Transient(f"no answer after {_ATTEMPTS} tries within {self.timeout_seconds:.0f}s"
                         f" ({last or 'budget spent'})")

    def _warm(self, url: str, served_model_name: str, deadline: float) -> tuple[int, float, str]:
        """The burst: how many answered, the slowest answer's seconds, and the
        last error. Run in parallel so every rank is busy at once."""
        payload = {
            "model": served_model_name,
            "messages": [{"role": "user", "content": _PROBE_PROMPT}],
            "max_tokens": _WARMUP_MAX_TOKENS,
            "temperature": 0,
        }

        def one(_: int) -> tuple[bool, float, str]:
            t0 = time.monotonic()
            try:
                response = self._post(url, payload, deadline)
            except _Transient as exc:
                return False, time.monotonic() - t0, str(exc)
            if response.status_code != 200:
                error = f"{response.status_code}: {response.text[:200]}"
                return False, time.monotonic() - t0, error
            return True, time.monotonic() - t0, ""

        with ThreadPoolExecutor(max_workers=_WARMUP_REQUESTS) as pool:
            results = list(pool.map(one, range(_WARMUP_REQUESTS)))
        answered = sum(1 for ok, _, _ in results if ok)
        slowest = max((s for _, s, _ in results), default=0.0)
        error = next((e for ok, _, e in results if not ok), "")
        return answered, slowest, error

    def _sanity(
        self, url: str, served_model_name: str, deadline: float
    ) -> tuple[int, int, list[dict[str, Any]]]:
        """Ask the sanity questions at once: how many came back right, how
        many were asked, and each one's expected and given answer."""
        asked = sanity_questions()[: self.sanity_count]

        def one(item: tuple[str, float]) -> dict[str, Any]:
            question, expected = item
            row: dict[str, Any] = {"question": question[:80], "expected": expected}
            try:
                response = self._post(url, {
                    "model": served_model_name,
                    "messages": [{"role": "user", "content": question + _SANITY_SUFFIX}],
                    "max_tokens": self.sanity_max_tokens,
                    "temperature": 0,
                }, deadline)
            except _Transient as exc:
                return {**row, "error": str(exc)[:200]}
            if response.status_code != 200:
                return {**row, "error": f"{response.status_code}: {response.text[:200]}"}
            message = ((response.json().get("choices") or [{}])[0]).get("message") or {}
            content = message.get("content") or ""
            reasoning = message.get("reasoning_content") or ""
            given = final_number(content, reasoning)
            return {**row, "given": given, "correct": given is not None
                    and abs(given - expected) < 1e-6,
                    "answer": (content or reasoning)[-160:]}

        with ThreadPoolExecutor(max_workers=max(len(asked), 1)) as pool:
            rows = list(pool.map(one, asked))
        return sum(1 for r in rows if r.get("correct")), len(rows), rows

    def _check(self, endpoint_url: str, served_model_name: str) -> EvalOutcome:
        deadline = time.monotonic() + self.timeout_seconds
        url = f"{endpoint_url}/v1/chat/completions"
        try:
            models_response = httpx.get(f"{endpoint_url}/v1/models", timeout=10)
            if models_response.status_code != 200:
                return EvalOutcome(
                    status=EvalStatus.FAILED,
                    error=f"/v1/models returned {models_response.status_code}",
                )

            completion_response = self._post(
                url,
                {
                    "model": served_model_name,
                    "messages": [{"role": "user", "content": _PROBE_PROMPT}],
                    # Reasoning models spend tokens thinking before they answer;
                    # a tiny budget yields an empty `content` and a false alarm.
                    "max_tokens": _PROBE_MAX_TOKENS,
                    "temperature": 0,
                },
                deadline,
            )
            if completion_response.status_code != 200:
                return EvalOutcome(
                    status=EvalStatus.FAILED,
                    error=f"chat completion returned {completion_response.status_code}: "
                    f"{completion_response.text[:500]}",
                )
            body = completion_response.json()
            choice = (body.get("choices") or [{}])[0]
            message = choice.get("message", {}) or {}
            content = (message.get("content") or "").strip()
            # sglang/vllm put a reasoning model's chain in a separate field;
            # output there still proves the engine is generating.
            reasoning = (message.get("reasoning_content") or "").strip()
            finish_reason = choice.get("finish_reason", "")

            if not content and not reasoning:
                return EvalOutcome(
                    status=EvalStatus.FAILED,
                    error=f"empty completion (finish_reason={finish_reason or 'unknown'})",
                    raw={"response": body},
                )
            if looping(content) or looping(reasoning):
                sample = (content or reasoning)[:120]
                return EvalOutcome(
                    status=EvalStatus.FAILED,
                    error=f"the engine answers in a loop, not in words ({sample!r}…): "
                    "its output is garbage, however fast it comes",
                    raw={"response": body, "failure_class": GARBAGE_OUTPUT},
                )
            answered, slowest, warm_error = self._warm(url, served_model_name, deadline)
            if not answered:
                # The probe answered but nothing of the burst did: the engine
                # cannot serve a handful of requests at once, which the
                # benchmark would find out the slow way.
                return EvalOutcome(
                    status=EvalStatus.FAILED,
                    error=f"warm-up: none of {_WARMUP_REQUESTS} concurrent requests answered "
                    f"({warm_error})",
                )
            sanity: dict[str, Any] = {}
            if self.sanity_count:
                correct, asked, rows = self._sanity(url, served_model_name, deadline)
                sanity = {"sanity_correct": correct, "sanity_asked": asked}
                answered_any = [r for r in rows if "error" not in r]
                if not answered_any:
                    # Nothing came back at all: the engine is down or out of
                    # time, which is not a verdict on its output.
                    return EvalOutcome(
                        status=EvalStatus.FAILED,
                        error=f"sanity questions: none answered ({rows[0].get('error', '')})",
                    )
                if correct < self.sanity_pass_rate * asked:
                    wrong = next((r for r in rows if not r.get("correct")), {})
                    return EvalOutcome(
                        status=EvalStatus.FAILED,
                        error=f"the engine got {correct} of {asked} grade-school math questions "
                        f"right (it needs {self.sanity_pass_rate * asked:.0f}; a working model "
                        f"gets nearly all): its output is garbage, however fast it comes. "
                        f"E.g. {wrong.get('question', '')!r}: expected {wrong.get('expected')}, "
                        f"got {wrong.get('given')} from {wrong.get('answer', '')[-80:]!r}",
                        metrics=sanity,
                        raw={"sanity": rows, "failure_class": GARBAGE_OUTPUT},
                    )
            usage = body.get("usage") or {}
            return EvalOutcome(
                status=EvalStatus.PASSED,
                metrics={
                    "probe_output_chars": len(content),
                    "probe_reasoning_chars": len(reasoning),
                    "probe_completion_tokens": usage.get("completion_tokens", 0),
                    "warmup_answered": answered,
                    "warmup_slowest_seconds": round(slowest, 1),
                    **sanity,
                },
                raw={
                    "probe_output": content[:200],
                    "probe_reasoning": reasoning[:200],
                    "finish_reason": finish_reason,
                    **({"warmup_error": warm_error} if warm_error else {}),
                },
            )
        except _Transient as exc:
            return EvalOutcome(status=EvalStatus.FAILED, error=f"chat completion: {exc}")
        except (httpx.HTTPError, json.JSONDecodeError, KeyError, IndexError) as exc:
            return EvalOutcome(status=EvalStatus.FAILED, error=f"{type(exc).__name__}: {exc}")
