"""Lightweight health/availability gate — the only evaluation the platform
runs itself. Checks that the service is up, answers a
completion, and the output is not obviously broken; then warms it up, so the
benchmark that follows meets an engine that has served before."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
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


class _Transient(Exception):
    pass


class HealthEvaluator(Evaluator):
    name = "health"

    def __init__(self, timeout_seconds: float = 60.0):
        # The whole gate's budget: the probe, its retries and the warm-up.
        self.timeout_seconds = timeout_seconds
        self._outcomes: dict[str, EvalOutcome] = {}

    def start(self, endpoint_url: str, served_model_name: str, context: dict[str, Any]) -> str:
        ref = f"health:{endpoint_url}"
        self._outcomes[ref] = self._check(endpoint_url, served_model_name)
        return ref

    def poll(self, external_ref: str) -> EvalOutcome:
        return self._outcomes.get(
            external_ref,
            # After a worker restart the in-memory outcome is gone; health is
            # cheap, so a missing ref simply means "re-run start()".
            EvalOutcome(status=EvalStatus.FAILED, error="health outcome lost; re-run"),
        )

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
            usage = body.get("usage") or {}
            return EvalOutcome(
                status=EvalStatus.PASSED,
                metrics={
                    "probe_output_chars": len(content),
                    "probe_reasoning_chars": len(reasoning),
                    "probe_completion_tokens": usage.get("completion_tokens", 0),
                    "warmup_answered": answered,
                    "warmup_slowest_seconds": round(slowest, 1),
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
