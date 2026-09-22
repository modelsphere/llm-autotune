"""Mock LLMBench — a fake external benchmark platform for flow testing.

Implements the subset of the LLMBench HTTP API the adapter uses (login,
submit, poll, cancel). A submission actually exercises the target endpoint in
a background thread (models list + a few chat completions), then "benchmarks"
for MOCK_LLMBENCH_SECONDS before reporting deterministic fake metrics — so an
unreachable or broken endpoint fails the submission, like the real platform.

Run standalone:
    uv run uvicorn app.mocks.llmbench_mock:app --port 28101
Point the worker at it:
    AUTOTUNE_LLMBENCH_BASE_URL=http://127.0.0.1:28101
"""

import hashlib
import os
import threading
import time
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Mock LLMBench")

_lock = threading.Lock()
_submissions: dict[int, dict[str, Any]] = {}
_next_id = 1


def _bench_seconds() -> float:
    return float(os.environ.get("MOCK_LLMBENCH_SECONDS", "15"))


def _busy_status() -> int:
    """MOCK_LLMBENCH_BUSY=429|503 makes submit reject like a quota/cordon."""
    return int(os.environ.get("MOCK_LLMBENCH_BUSY", "0"))


class LoginBody(BaseModel):
    username: str = ""
    password: str = ""


class SubmitBody(BaseModel):
    """Mirrors LLMBench's SubmissionCreate (the fields we send)."""

    endpoint_url: str
    model: str
    api_key: str = ""
    description_summary: str | None = None
    description_detail: str | None = None
    cards_per_machine: int | None = None
    machine_count: int | None = None
    card_type: str | None = None
    extra_params: dict[str, Any] | None = None


@app.post("/auth/login")
def login(body: LoginBody):
    return {"access_token": "mock-token", "token_type": "bearer"}


def _probe_endpoint(submission: dict[str, Any]) -> None:
    """Hit the target endpoint like a benchmark would; record pass/fail."""
    endpoint_url = submission["endpoint_url"]
    model = submission["endpoint_model"]
    try:
        response = httpx.get(f"{endpoint_url}/v1/models", timeout=10)
        response.raise_for_status()
        for _ in range(3):
            completion = httpx.post(
                f"{endpoint_url}/v1/chat/completions",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "mock benchmark request"}],
                    "max_tokens": 32,
                },
                timeout=30,
            )
            completion.raise_for_status()
        submission["probe_ok"] = True
    except httpx.HTTPError as exc:
        submission["probe_ok"] = False
        submission["error"] = f"endpoint probe failed: {exc}"


def _fake_metrics(submission_id: int) -> dict[str, Any]:
    # Deterministic pseudo-metrics from the submission id.
    seed = int(hashlib.sha256(str(submission_id).encode()).hexdigest()[:8], 16)
    output_tps = 800 + seed % 900
    # Like the real platform: a submission that RAN reports status=done and
    # carries its verdict separately in `passed`. MOCK_LLMBENCH_NOT_PASSED=1
    # simulates a redline breach (done + passed=false).
    not_passed = os.environ.get("MOCK_LLMBENCH_NOT_PASSED") == "1"
    return {
        "score_total": float(output_tps),
        "passed": not not_passed,
        "runs": [
            {
                "module_name": "perf_mock",
                "status": "done",
                "passed": not not_passed,
                "error": "latency redline breached" if not_passed else None,
                "metrics_json": {
                    "output_tps": output_tps,
                    "ttft_ms_p99": 120 + seed % 300,
                    "itl_ms_p95": 8 + seed % 25,
                },
            }
        ],
    }


class PreflightBody(BaseModel):
    endpoint_url: str
    model: str
    api_key: str = ""


@app.post("/submissions/preflight")
def preflight(body: PreflightBody):
    """Mirrors LLMBench's endpoint probe: reachable + serves the model?"""
    checks: list[dict[str, str]] = []
    try:
        response = httpx.get(f"{body.endpoint_url}/v1/models", timeout=10)
        response.raise_for_status()
        checks.append({"name": "models", "status": "pass", "detail": "endpoint reachable"})
    except httpx.HTTPError as exc:
        checks.append({"name": "models", "status": "fail", "detail": str(exc)[:200]})
        return {"ok": False, "endpoint_tested": body.endpoint_url, "checks": checks}
    try:
        completion = httpx.post(
            f"{body.endpoint_url}/v1/chat/completions",
            json={
                "model": body.model,
                "messages": [{"role": "user", "content": "preflight"}],
                "max_tokens": 8,
            },
            timeout=30,
        )
        completion.raise_for_status()
        checks.append({"name": "completion", "status": "pass", "detail": "model answered"})
        ok = True
    except httpx.HTTPError as exc:
        checks.append({"name": "completion", "status": "fail", "detail": str(exc)[:200]})
        ok = False
    return {"ok": ok, "endpoint_tested": body.endpoint_url, "checks": checks}


@app.post("/submissions/benchmarks/{slug}/submit", status_code=202)
def submit(slug: str, body: SubmitBody):
    global _next_id
    busy = _busy_status()
    if busy:
        raise HTTPException(
            busy,
            "Active submission limit reached (8/8)."
            if busy == 429
            else "Platform cordoned for maintenance.",
        )
    with _lock:
        submission_id = _next_id
        _next_id += 1
        submission = {
            "id": submission_id,
            "slug": slug,
            "endpoint_url": body.endpoint_url,
            "endpoint_model": body.model,
            "description_summary": body.description_summary,
            "cards_per_machine": body.cards_per_machine,
            "created": time.monotonic(),
            "canceled": False,
            "probe_ok": None,  # None = probe still running
            "error": "",
        }
        _submissions[submission_id] = submission
    threading.Thread(target=_probe_endpoint, args=(submission,), daemon=True).start()
    return {"id": submission_id, "status": "queued"}


@app.get("/submissions/{submission_id}")
def get_submission(submission_id: int):
    submission = _submissions.get(submission_id)
    if submission is None:
        raise HTTPException(404, "no such submission")
    if submission["canceled"]:
        return {"id": submission_id, "status": "canceled", "error": "canceled"}
    if submission["probe_ok"] is False:
        return {"id": submission_id, "status": "failed", "error": submission["error"]}
    elapsed = time.monotonic() - submission["created"]
    if submission["probe_ok"] is None or elapsed < 2:
        return {"id": submission_id, "status": "queued"}
    if elapsed < _bench_seconds():
        return {"id": submission_id, "status": "running"}
    return {"id": submission_id, "status": "done", **_fake_metrics(submission_id)}


@app.post("/submissions/{submission_id}/cancel")
def cancel(submission_id: int):
    submission = _submissions.get(submission_id)
    if submission is None:
        raise HTTPException(404, "no such submission")
    submission["canceled"] = True
    return {"ok": True}
