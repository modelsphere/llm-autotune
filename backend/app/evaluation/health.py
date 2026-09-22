"""Lightweight health/availability gate — the only in-platform evaluation in
the PoC (poc-scope.md decision 4). Checks that the service is up, answers a
completion, and the output is not obviously broken."""

import json
from typing import Any

import httpx

from app.evaluation.base import EvalOutcome, EvalStatus, Evaluator

_PROBE_PROMPT = "Reply with exactly the word OK."
# Generous enough that a reasoning model can think AND answer.
_PROBE_MAX_TOKENS = 512


class HealthEvaluator(Evaluator):
    name = "health"

    def __init__(self, timeout_seconds: float = 60.0):
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

    def _check(self, endpoint_url: str, served_model_name: str) -> EvalOutcome:
        try:
            models_response = httpx.get(f"{endpoint_url}/v1/models", timeout=10)
            if models_response.status_code != 200:
                return EvalOutcome(
                    status=EvalStatus.FAILED,
                    error=f"/v1/models returned {models_response.status_code}",
                )

            completion_response = httpx.post(
                f"{endpoint_url}/v1/chat/completions",
                json={
                    "model": served_model_name,
                    "messages": [{"role": "user", "content": _PROBE_PROMPT}],
                    # Reasoning models spend tokens thinking before they answer;
                    # a tiny budget yields an empty `content` and a false alarm.
                    "max_tokens": _PROBE_MAX_TOKENS,
                    "temperature": 0,
                },
                timeout=self.timeout_seconds,
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
            usage = body.get("usage") or {}
            return EvalOutcome(
                status=EvalStatus.PASSED,
                metrics={
                    "probe_output_chars": len(content),
                    "probe_reasoning_chars": len(reasoning),
                    "probe_completion_tokens": usage.get("completion_tokens", 0),
                },
                raw={
                    "probe_output": content[:200],
                    "probe_reasoning": reasoning[:200],
                    "finish_reason": finish_reason,
                },
            )
        except (httpx.HTTPError, json.JSONDecodeError, KeyError, IndexError) as exc:
            return EvalOutcome(status=EvalStatus.FAILED, error=f"{type(exc).__name__}: {exc}")
