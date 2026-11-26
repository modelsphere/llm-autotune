"""Mock LLMBench — a fake benchmark platform for flow testing.

Implements the part of LLMBench's HTTP API the adapter uses, in the SAME shape
the real platform answers with (LLMBench docs/api/for-autotune.md): login,
the benchmark catalog, preflight, submit, poll, cancel, and the rolling-dataset
routes dataset pinning reads. A submission really exercises the target endpoint
in a background thread (models list + a few chat completions), then
"benchmarks" for MOCK_LLMBENCH_SECONDS before reporting deterministic fake
metrics — so an unreachable or broken endpoint fails the submission, like the
real platform. Nothing it reports measures anything.

Run standalone:
    uv run uvicorn app.mocks.llmbench_mock:app --port 28101
Point the worker at it:
    AUTOTUNE_LLMBENCH_BASE_URL=http://127.0.0.1:28101

Knobs:
    MOCK_LLMBENCH_SECONDS=15      how long a submission "runs"
    MOCK_LLMBENCH_BUSY=429|503    submit refuses like the quota / a cordon
    MOCK_LLMBENCH_NOT_PASSED=1    every run breaches its redline (done, passed=false)
"""

import email
import email.policy
import gzip
import hashlib
import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

app = FastAPI(title="Mock LLMBench")

_lock = threading.Lock()
_submissions: dict[int, dict[str, Any]] = {}
_next_id = 1

# Card normalization baseline, as LLMBench's throughput modules declare it.
CARD_NORM_BASELINE = 8

# --- the benchmark catalog --------------------------------------------------------
# Two benchmarks, in the shape GET /benchmarks lists them: a screen (one sweep)
# and a verify (sweep + a replay pinned to a rolling profile).

SWEEP_CONFIGS = [
    {"key": "output_tps", "role": "score", "weight": 1.0, "formula": "ratio_capped",
     "baseline": 2000.0, "min_val": None, "max_val": None},
    {"key": "uptime", "role": "redline", "min_val": 0.95, "max_val": None},
    {"key": "ttft_p99_ms", "role": "display", "min_val": None, "max_val": None},
]
REPLAY_CONFIGS = [
    {"key": "uptime", "role": "score", "weight": 1.0, "formula": "passthrough",
     "min_val": None, "max_val": None},
    {"key": "uptime", "role": "redline", "min_val": 0.95, "max_val": None},
]
SWEEP_PARAMS = {"search_mode": "grid", "concurrencies": "1,4,16",
                "input_tokens": 1024, "output_tokens": 256}
PROFILE = {"id": 1, "name": "prod-traffic-sample", "display_name": "Production sample"}

BENCHMARKS: dict[str, list[dict[str, Any]]] = {
    # What AutoTune ensures for itself (app/evaluation/benchmarks.py). Listed as
    # already created by this account, so an ensure against the mock is a no-op.
    "autotune-screen-v1": [
        {"id": 1, "module_name": "perf_guidellm_sweep", "params_json": SWEEP_PARAMS,
         "metric_configs": SWEEP_CONFIGS, "weight": 1.0, "order_index": 0},
    ],
    "perf-suite-v1": [
        {"id": 11, "module_name": "perf_guidellm_sweep", "params_json": SWEEP_PARAMS,
         "metric_configs": SWEEP_CONFIGS, "weight": 1.0, "order_index": 0},
    ],
    "replay-suite-v1": [
        {"id": 21, "module_name": "perf_guidellm_sweep", "params_json": SWEEP_PARAMS,
         "metric_configs": SWEEP_CONFIGS, "weight": 0.5, "order_index": 0},
        {"id": 22, "module_name": "replay",
         "params_json": {"dataset_source": "auto", "dataset_profile": PROFILE["name"]},
         "metric_configs": REPLAY_CONFIGS, "weight": 0.5, "order_index": 1},
    ],
}


def _bench_seconds() -> float:
    return float(os.environ.get("MOCK_LLMBENCH_SECONDS", "15"))


def _busy_status() -> int:
    return int(os.environ.get("MOCK_LLMBENCH_BUSY", "0"))


class LoginBody(BaseModel):
    email: str = ""
    password: str = ""


class SubmitBody(BaseModel):
    """Mirrors LLMBench's SubmissionCreate."""

    endpoint_url: str
    model: str
    api_key: str = ""
    contributor: str | None = None
    description_summary: str | None = None
    description_detail: str | None = None
    cards_per_machine: int | None = None
    machine_count: int | None = None
    card_type: str | None = None
    source_url: str | None = None
    extra_params: dict[str, Any] | None = None


@app.post("/auth/login")
def login(body: LoginBody):
    return {"access_token": "mock-token", "token_type": "bearer", "role": "service"}


# The service account AutoTune acts as; every benchmark in the catalog is its own.
MOCK_ACCOUNT = {"id": 1, "username": "autotune", "email": "autotune@service.invalid",
                "role": "service"}
_locked: set[str] = set(BENCHMARKS)


@app.get("/auth/me")
def me():
    return MOCK_ACCOUNT


# Group tags per slug, as LLMBench files benchmarks in its list.
_group_tags: dict[str, list[str]] = {}


def _benchmark_out(slug: str) -> dict[str, Any]:
    return {"id": list(BENCHMARKS).index(slug) + 1, "slug": slug, "name": slug,
            "status": "active", "is_locked": slug in _locked,
            "group_tags": _group_tags.get(slug, []),
            "created_by_user_id": MOCK_ACCOUNT["id"],
            "config_hash": hashlib.sha256(slug.encode()).hexdigest()[:8],
            "modules": [{k: v for k, v in m.items() if k != "id"} for m in BENCHMARKS[slug]]}


@app.post("/benchmarks/admin/benchmarks/import")
async def import_benchmark(request: Request):
    # The upload is one multipart file; read it with the stdlib rather than make
    # python-multipart a dependency for a mock.
    header = f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
    raw = header + await request.body()
    part = next(iter(email.message_from_bytes(raw, policy=email.policy.HTTP).iter_parts()))
    doc = yaml.safe_load(part.get_payload(decode=True))
    slug = doc["slug"]
    if slug in BENCHMARKS:
        raise HTTPException(409, f"Benchmark slug '{slug}' already exists")
    BENCHMARKS[slug] = [
        {"id": 100 + len(BENCHMARKS) * 10 + i, "module_name": m["module_name"],
         "params_json": m.get("params") or {}, "metric_configs": m.get("metric_configs") or [],
         "weight": m.get("weight", 1.0), "order_index": i}
        for i, m in enumerate(doc.get("modules") or [])
    ]
    _group_tags[slug] = list(doc.get("group_tags") or [])
    return _benchmark_out(slug)


@app.put("/benchmarks/admin/benchmarks/{benchmark_id}")
async def update_benchmark(benchmark_id: int, request: Request):
    """Only grouping, the one change LLMBench takes on a locked benchmark."""
    slug = list(BENCHMARKS)[benchmark_id - 1]
    body = await request.json()
    if body.get("group_tags") is not None:
        _group_tags[slug] = list(body["group_tags"])
    return _benchmark_out(slug)


@app.get("/modules/{name}")
def module_descriptor(name: str):
    from app.evaluation.benchmark_spec import BFCLSpec, bfcl_module

    configs = {"replay": REPLAY_CONFIGS, "perf_guidellm_sweep": SWEEP_CONFIGS,
               "bfcl_v4": bfcl_module(BFCLSpec())["metric_configs"]}.get(name)
    if configs is None:
        raise HTTPException(404, "Module not found")
    return {"name": name, "default_params": {}, "default_metric_configs": configs}


@app.put("/benchmarks/admin/benchmarks/{benchmark_id}/lock")
def toggle_lock(benchmark_id: int):
    slug = list(BENCHMARKS)[benchmark_id - 1]
    _locked.symmetric_difference_update({slug})
    return _benchmark_out(slug)


@app.get("/benchmarks")
def list_benchmarks():
    return {"benchmarks": [_benchmark_out(slug) for slug in BENCHMARKS]}


@app.get("/benchmarks/{slug}")
def get_benchmark(slug: str):
    if slug not in BENCHMARKS:
        raise HTTPException(404, "Benchmark not found")
    return _benchmark_out(slug)


# --- rolling datasets: one profile whose build is always ready ------------------------

_builds: dict[int, dict[str, Any]] = {}
_current_build: dict[str, Any] = {}


def _new_build() -> dict[str, Any]:
    now = datetime.now(UTC)
    build_id = now.strftime("%Y%m%dT%H%M%SZ")
    row = {
        "id": len(_builds) + 1, "profile_id": PROFILE["id"], "status": "ready",
        "build_id": build_id, "sha256": hashlib.sha256(build_id.encode()).hexdigest(),
        "records": 2000, "finished_at": now.isoformat(), "error": None,
        "window_start": (now - timedelta(hours=24)).isoformat(), "window_end": now.isoformat(),
    }
    _builds[row["id"]] = row
    _current_build.clear()
    _current_build.update(row)
    return row


def _load_reading() -> dict[str, Any]:
    """A quiet backend, as LLMBench's load reading reports one: the same
    request barely slows with load, no knee within what production reached."""
    groups, above = [], 5200
    for g, n, s in ((1, 3100, 1.0), (2, 1500, 1.03), (3, 600, 1.08)):
        groups.append({"group": g, "requests": n, "c_mean": g + 0.02, "share_from": above / 5200,
                       "slowdown": s, "raw_slowdown": s, "e_into": None if g == 1 else 0.9,
                       "latency_median_s": 2.1 * s, "prompt_mean": 14000, "output_mean": 300,
                       "cached_share": 0.6})
        above -= n
    return {"version": 1, "status": "ok", "category": "idle", "fitted_category": "idle",
            "knee_group": None, "w_log": 0.0, "crossings": 0, "requests": 5200, "c_typ": 1.3,
            "c_peak": 2.1, "c_req_max": 3.4, "window_hours": 168, "groups": groups,
            "thresholds": {"idle_below": 0.1, "busy_from": 0.5, "min_group": 300},
            "fit": {"r2_log": 0.81, "residual_by_utc_block": {}, "size_coefficients_s": {},
                    "halves": {"even_days": {"knee_group": None, "w_log": 0.0, "category": "idle"},
                               "odd_days": {"knee_group": None, "w_log": 0.0, "category": "idle"}},
                    "checks": {"r2": True, "no_drift_by_time_of_day": True, "halves_agree": True,
                               "single_crossing": True}}}


def _profile_out() -> dict[str, Any]:
    if not _current_build:
        _new_build()
    keys = ("build_id", "sha256", "records", "window_start", "window_end")
    current = {k: _current_build[k] for k in keys}
    current["built_at"] = _current_build["finished_at"]
    current["load"] = _load_reading()
    return {**PROFILE, "enabled": True, "schedule_interval_hours": 0, "current": current}


@app.get("/replay-datasets/profiles")
def list_profiles():
    return {"profiles": [_profile_out()], "collector_configured": True}


@app.get("/replay-datasets/profiles/{profile_id}")
def get_profile(profile_id: int):
    if profile_id != PROFILE["id"]:
        raise HTTPException(404, "no such profile")
    return _profile_out()


@app.post("/replay-datasets/profiles/{profile_id}/build")
def trigger_build(profile_id: int):
    if profile_id != PROFILE["id"]:
        raise HTTPException(404, "no such profile")
    return {"build_row_id": _new_build()["id"]}


@app.get("/replay-datasets/profiles/{profile_id}/builds")
def list_builds(profile_id: int, limit: int = 20):
    rows = sorted(_builds.values(), key=lambda r: r["id"], reverse=True)[:limit]
    return {"builds": rows}


# One frozen dataset, as an import from another installation would leave it.
FROZEN = {
    "name": "mock-frozen", "path": "/app/dataset/replay/frozen/mock-frozen.jsonl.gz",
    "bytes": 2048, "records": 20, "sha256": "f" * 64,
    "frozen_at": "2026-01-01T00:00:00Z", "source_profile": "mock-traffic",
    "source_build_id": "20260101T000000Z", "window_start": None, "window_end": None,
    "frozen_by": "admin", "origin": "upload", "used_by": [],
}


IMPORTED: list[dict[str, Any]] = []


@app.get("/replay-datasets/frozen")
def list_frozen():
    return {"frozen": [FROZEN, *IMPORTED], "collector_configured": True}


@app.put("/replay-datasets/frozen/{name}", status_code=201)
async def import_frozen(name: str, request: Request):
    """LLMBench's import: the body is the file; a taken name is a 409, an
    empty or unreadable one a 422 (here: any line that is not JSON)."""
    if name == FROZEN["name"] or any(d["name"] == name for d in IMPORTED):
        raise HTTPException(409, f"A frozen dataset named {name!r} exists")
    body = await request.body()
    raw = gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body
    lines = [line for line in raw.splitlines() if line.strip()]
    try:
        for line in lines:
            json.loads(line)
    except ValueError:
        raise HTTPException(422, "not a replay dataset: a line is not JSON") from None
    if not lines:
        raise HTTPException(422, "the request body is empty")
    entry = {**FROZEN, "name": name, "path": f"/app/dataset/replay/frozen/{name}.jsonl",
             "bytes": len(raw), "records": len(lines), "sha256": hashlib.sha256(raw).hexdigest(),
             "origin": "upload", "source_profile": "", "source_build_id": ""}
    IMPORTED.append(entry)
    return entry


@app.get("/replay-datasets/builds/{build_row_id}")
def get_build(build_row_id: int):
    row = _builds.get(build_row_id)
    if row is None:
        raise HTTPException(404, "no such build")
    return row


# --- submissions -------------------------------------------------------------------


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


def _cards(submission: dict[str, Any]) -> int:
    total = (submission.get("cards_per_machine") or 0) * (submission.get("machine_count") or 0)
    # Missing hardware is assumed to be at the baseline, as the real one does.
    return total or CARD_NORM_BASELINE


def _sweep_metrics(seed: int, cards: int) -> dict[str, Any]:
    output_tps = 800.0 + seed % 900
    input_tps = output_tps * 4
    return {
        "input_tps": input_tps,
        "output_tps": output_tps,
        "total_tps_mean": input_tps + output_tps,
        "request_output_tps": output_tps / 16,
        "ttft_p50_ms": 80.0 + seed % 100,
        "ttft_p90_ms": 100.0 + seed % 200,
        "ttft_p99_ms": 120.0 + seed % 300,
        "itl_p99_ms": 8.0 + seed % 25,
        "tpot_p99_ms": 9.0 + seed % 25,
        "uptime": 1.0,
        "reported_concurrency": 16,
        "reported_level_meets_slo": True,
        # Added by LLMBench's worker, from the declared hardware.
        "output_tpm_card_norm": output_tps * 60 * CARD_NORM_BASELINE / cards,
        "input_tpm_card_norm": input_tps * 60 * CARD_NORM_BASELINE / cards,
        "total_tpm_card_norm": (input_tps + output_tps) * 60 * CARD_NORM_BASELINE / cards,
        "c16": {"output_tps": output_tps, "ttft_p99_ms": 120.0 + seed % 300},
    }


def _replay_metrics(seed: int, cards: int) -> dict[str, Any]:
    output_tpm = (600.0 + seed % 400) * 60
    return {
        "uptime": 1.0,
        "error_rate": 0.0,
        "unfinished_rate": 0.0,
        "input_tpm": output_tpm * 6,
        "uncached_input_tpm": output_tpm * 2,
        "cached_tpm": output_tpm * 4,
        "output_tpm": output_tpm,
        "cache_hit_rate": 0.66,
        "ttft_p50_ms": 300.0 + seed % 200,
        "ttft_p99_ms": 900.0 + seed % 600,
        "total_time_p99_ms": 20000.0 + seed % 5000,
        "http_200_count": 2000.0,
        "output_tpm_card_norm": output_tpm * CARD_NORM_BASELINE / cards,
        # Which build this run replayed: LLMBench stamps the build id and the
        # FIRST 16 hex characters of its sha256.
        "dataset_id": _current_build.get("build_id", ""),
        "dataset_sha256": _current_build.get("sha256", "")[:16],
    }


def _timed_metrics(seed: int) -> dict[str, Any]:
    """A timed replay (1x load): latency from each request's due time; the
    schedule sets the throughput, so the flat TPM keys are empty."""
    e2e = 3000.0 + seed % 600
    return {
        "e2e_p50_ms": e2e, "e2e_p90_ms": e2e * 2.5, "e2e_mean_ms": e2e * 1.3,
        "ttft_p50_ms": 250.0 + seed % 100, "ttft_p90_ms": 700.0 + seed % 200,
        "tpot_p50_ms": 18.0 + seed % 5,
        "session_time_p50_s": e2e * 6 / 1000, "sessions_measured": 40,
        "measured_requests": 380, "warmup_requests": 20, "schedule_s": 420.0,
        "replay_s": 431.0, "schedule_lag_p99_s": 0.02, "fell_behind": 0,
        "max_in_flight_hit": 0, "in_flight_mean": 2.1, "in_flight_max": 9,
        "cache_flushed": 1,
        # equivalent throughput: faster (lower e2e) serves more
        "equivalent_tpm": 9.0e8 / e2e, "equivalent_tpm_logged": 3.0e5,
        "equivalent_coverage": 0.93, "equivalent_busy_share": 0.71,
        "equivalent_size_matched": 1, "equivalent_size_r2": 0.82,
        "equivalent_little_error": 0.02, "equivalent_levels": 7,
        "input_tpm": None, "output_tpm": None, "cached_tpm": None, "uncached_input_tpm": None,
    }


def _bfcl_metrics(seed: int, params: dict) -> dict[str, Any]:
    task_set = params.get("task_set", "bfcl-v4-tool-use-v1")
    categories = (["multi_turn_long_context"] if "long-context" in task_set else
                  ["simple_python", "multi_turn_base"] if "smoke" in task_set else
                  ["simple_python", "multiple", "parallel", "irrelevance", "multi_turn_base"])
    n = params.get("cases_per_category", 1)
    count = n * len(categories)
    passed = int(count * (0.7 + (seed % 20) / 100))
    accuracy = passed / count
    wall = 60.0
    return {
        "accuracy": accuracy, **{f"accuracy_{c}": accuracy for c in categories},
        "case_count": count, **{f"case_count_{c}": n for c in categories},
        "passed_cases_per_hour": passed * 3600 / wall,
        "generation_wall_seconds": wall, "grading_wall_seconds": 2.0,
        "ttft_p50_ms": 120.0, "ttft_p90_ms": 250.0, "ttft_p99_ms": 400.0,
        "request_e2e_p90_ms": 1500.0, "input_tpm": 6000.0, "output_tpm": 1200.0,
        "total_tpm": 7200.0, "cache_hit_rate": 0.25, "ttft_coverage": 1.0,
        "input_usage_coverage": 1.0, "output_usage_coverage": 1.0,
        "cache_usage_coverage": 1.0, "active_cases_peak": params.get("concurrency", 2),
        "active_http_peak": params.get("concurrency", 2), "request_error_count": 0,
        "retry_count": 0, "infra_error_count": 0, "http_attempt_count": count * 2,
        "successful_request_rps": count * 2 / wall,
    }


def _runs(submission: dict[str, Any], not_passed: bool) -> list[dict[str, Any]]:
    seed = int(hashlib.sha256(str(submission["id"]).encode()).hexdigest()[:8], 16)
    cards = _cards(submission)
    runs = []
    for mod in BENCHMARKS.get(submission["slug"], BENCHMARKS["autotune-screen-v1"]):
        if mod["module_name"] == "replay":
            if not _current_build:
                _new_build()
            metrics = _replay_metrics(seed, cards)
            if (mod["params_json"] or {}).get("schedule") == "timed":
                metrics = {**metrics, **_timed_metrics(seed)}
            params = {**mod["params_json"], "dataset_resolved": {
                "build_id": _current_build["build_id"], "sha256": _current_build["sha256"]}}
        elif mod["module_name"] == "bfcl_v4":
            params = dict(mod["params_json"])
            metrics = _bfcl_metrics(seed, params)
        else:
            metrics = _sweep_metrics(seed, cards)
            params = dict(mod["params_json"])
        runs.append({
            "id": submission["id"] * 10 + mod["order_index"],
            "module_name": mod["module_name"],
            "benchmark_module_id": mod["id"],
            "params_json": params,
            "status": "done",
            "score": 0.9,
            "passed": not not_passed,
            "error": "uptime redline breached" if not_passed else None,
            "metrics_json": metrics,
            "metric_configs_json": mod["metric_configs"],
        })
    return runs


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
        checks.append({"name": "Connectivity", "status": "pass", "detail": "endpoint reachable"})
    except httpx.HTTPError as exc:
        checks.append({"name": "Connectivity", "status": "fail", "detail": str(exc)[:200]})
        return {"ok": False, "endpoint_tested": body.endpoint_url, "checks": checks}
    try:
        completion = httpx.post(
            f"{body.endpoint_url}/v1/chat/completions",
            json={"model": body.model, "messages": [{"role": "user", "content": "preflight"}],
                  "max_tokens": 8},
            timeout=30,
        )
        completion.raise_for_status()
        checks.append({"name": "Chat completion", "status": "pass", "detail": "model answered"})
        ok = True
    except httpx.HTTPError as exc:
        checks.append({"name": "Chat completion", "status": "fail", "detail": str(exc)[:200]})
        ok = False
    return {"ok": ok, "endpoint_tested": body.endpoint_url, "checks": checks}


@app.post("/submissions/benchmarks/{slug}/submit", status_code=202)
def submit(slug: str, body: SubmitBody):
    global _next_id
    busy = _busy_status()
    if busy:
        raise HTTPException(
            busy,
            "Active submission limit reached (8/8)." if busy == 429
            else "The platform is updating; new submissions are paused.",
        )
    if slug not in BENCHMARKS:
        raise HTTPException(404, "Benchmark not found")
    with _lock:
        submission_id = _next_id
        _next_id += 1
        submission = {
            "id": submission_id, "slug": slug,
            "endpoint_url": body.endpoint_url, "endpoint_model": body.model,
            "description_summary": body.description_summary,
            "cards_per_machine": body.cards_per_machine, "machine_count": body.machine_count,
            "card_type": body.card_type, "source_url": body.source_url,
            "created": time.monotonic(), "canceled": False,
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
    base = {"id": submission_id, "benchmark_slug": submission["slug"],
            "source_url": submission["source_url"], "runs": []}
    if submission["canceled"]:
        return {**base, "status": "canceled", "passed": None, "score_total": None,
                "error": "canceled"}
    if submission["probe_ok"] is False:
        return {**base, "status": "failed", "passed": None, "score_total": None,
                "error": submission["error"]}
    elapsed = time.monotonic() - submission["created"]
    if submission["probe_ok"] is None or elapsed < 2:
        return {**base, "status": "queued", "passed": None, "score_total": None, "error": None}
    if elapsed < _bench_seconds():
        return {**base, "status": "running", "passed": None, "score_total": None, "error": None}
    not_passed = os.environ.get("MOCK_LLMBENCH_NOT_PASSED") == "1"
    runs = _runs(submission, not_passed)
    return {**base, "status": "done", "passed": not not_passed, "score_total": 0.9,
            "error": None, "runs": runs}


@app.post("/submissions/{submission_id}/cancel")
def cancel(submission_id: int):
    submission = _submissions.get(submission_id)
    if submission is None:
        raise HTTPException(404, "no such submission")
    submission["canceled"] = True
    return {"ok": True}
