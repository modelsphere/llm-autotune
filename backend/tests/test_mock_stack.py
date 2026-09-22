"""Integration tests for the mock stack: real HTTP, real subprocesses.

- mock engine started via `python -m sglang.launch_server` (same command the
  driver renders) → probed with the real HealthEvaluator
- mock LLMBench served by uvicorn in a thread → driven by the real
  LLMBenchClient/LLMBenchEvaluator
"""

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from app.core.config import get_settings
from app.evaluation import (
    EvalStatus,
    EvaluatorBusy,
    EvaluatorRejected,
    HealthEvaluator,
    LLMBenchClient,
    LLMBenchEvaluator,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MOCK_ENGINE_DIR = REPO_ROOT / "mock-engine"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_ready(url: str, timeout: float = 15) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=2).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    return False


@pytest.fixture
def mock_engine():
    port = _free_port()
    env = {**os.environ, "PYTHONPATH": str(MOCK_ENGINE_DIR)}
    process = subprocess.Popen(
        [
            sys.executable, "-m", "sglang.launch_server",
            "--model-path", "/tmp/fake-model",
            "--served-model-name", "mock-model",
            "--host", "127.0.0.1",
            "--port", str(port),
            "--mock-startup-seconds", "0.2",
            "--mock-latency-ms", "10",
            "--tp-size", "4",  # unknown engine flag must be ignored
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    endpoint = f"http://127.0.0.1:{port}"
    assert _wait_ready(f"{endpoint}/v1/models"), "mock engine never became ready"
    yield endpoint
    process.kill()
    process.wait(timeout=10)


@pytest.fixture
def mock_llmbench(monkeypatch):
    monkeypatch.setenv("MOCK_LLMBENCH_SECONDS", "1")
    from app.mocks.llmbench_mock import app as mock_app

    port = _free_port()
    config = uvicorn.Config(mock_app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    assert _wait_ready(f"{base_url}/docs"), "mock llmbench never became ready"
    yield base_url
    server.should_exit = True
    thread.join(timeout=10)


def test_health_evaluator_against_mock_engine(mock_engine):
    evaluator = HealthEvaluator(timeout_seconds=15)
    ref = evaluator.start(mock_engine, "mock-model", {})
    outcome = evaluator.poll(ref)
    assert outcome.status == EvalStatus.PASSED, outcome.error
    assert "OK" in outcome.raw.get("probe_output", "")


def test_health_accepts_a_reasoning_model_with_empty_content(monkeypatch):
    """Regression: a real reasoning model put its output in reasoning_content
    and the canary declared the production service unhealthy."""
    import httpx as _httpx

    from app.evaluation import health as health_module

    def fake_get(url, **kwargs):
        return _httpx.Response(200, json={"data": [{"id": "m"}]})

    def fake_post(url, **kwargs):
        return _httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": "", "reasoning_content": "thinking… OK"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"completion_tokens": 12},
            },
        )

    monkeypatch.setattr(health_module.httpx, "get", fake_get)
    monkeypatch.setattr(health_module.httpx, "post", fake_post)

    evaluator = HealthEvaluator()
    outcome = evaluator.poll(evaluator.start("http://x", "m", {}))
    assert outcome.status == EvalStatus.PASSED, outcome.error
    assert outcome.metrics["probe_reasoning_chars"] > 0


def test_health_still_fails_when_nothing_is_generated(monkeypatch):
    import httpx as _httpx

    from app.evaluation import health as health_module

    monkeypatch.setattr(
        health_module.httpx, "get", lambda url, **kw: _httpx.Response(200, json={"data": []})
    )
    monkeypatch.setattr(
        health_module.httpx,
        "post",
        lambda url, **kw: _httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "", "reasoning_content": ""}, "finish_reason": "length"}
                ]
            },
        ),
    )
    evaluator = HealthEvaluator()
    outcome = evaluator.poll(evaluator.start("http://x", "m", {}))
    assert outcome.status == EvalStatus.FAILED
    assert "finish_reason=length" in outcome.error


def test_llmbench_adapter_against_mock(mock_engine, mock_llmbench):
    client = LLMBenchClient(base_url=mock_llmbench, api_key="llmb_testkey")
    evaluator = LLMBenchEvaluator(client=client, benchmark_slug="perf-suite-v1")

    ref = evaluator.start(
        mock_engine,
        "mock-model",
        {
            "description_summary": "autotune run 1: tp_size=4",
            "hardware": {"cards_per_machine": 8, "machine_count": 1, "card_type": "H200"},
        },
    )
    deadline = time.monotonic() + 20
    outcome = evaluator.poll(ref)
    while outcome.status == EvalStatus.RUNNING and time.monotonic() < deadline:
        time.sleep(0.3)
        outcome = evaluator.poll(ref)

    assert outcome.status == EvalStatus.PASSED, outcome.error
    assert outcome.metrics["score_total"] > 0
    assert "perf_mock.output_tps" in outcome.metrics


def test_api_key_needs_no_login_round_trip(mock_llmbench):
    """An llmb_ key is a bearer credential on its own — no /auth/login."""
    client = LLMBenchClient(base_url=mock_llmbench, api_key="llmb_testkey")
    assert client._token == "llmb_testkey"
    submission_id = client.submit("perf-suite-v1", "http://127.0.0.1:1", "m")
    assert submission_id  # 202 accepted, id returned


def test_preflight_rejects_a_dead_endpoint_before_submitting(mock_llmbench, monkeypatch):
    """Under JWT auth (the only mode LLMBench allows preflight in), a dead
    endpoint is caught by their probe — no submission is made."""
    monkeypatch.setenv("AUTOTUNE_LLMBENCH_PREFLIGHT", "true")
    get_settings.cache_clear()
    try:
        client = LLMBenchClient(base_url=mock_llmbench, api_key="", username="u", password="p")
        evaluator = LLMBenchEvaluator(client=client, benchmark_slug="perf-suite-v1")
        with pytest.raises(EvaluatorRejected, match="preflight"):
            evaluator.start("http://127.0.0.1:1", "m", {})
    finally:
        get_settings.cache_clear()


def test_preflight_is_skipped_for_api_key_auth(mock_llmbench, monkeypatch):
    """LLMBench 403s preflight for API keys by design — don't even ask, and
    never let that stop a submission."""
    monkeypatch.setenv("AUTOTUNE_LLMBENCH_PREFLIGHT", "true")
    get_settings.cache_clear()
    try:
        client = LLMBenchClient(base_url=mock_llmbench, api_key="llmb_testkey")
        evaluator = LLMBenchEvaluator(client=client, benchmark_slug="perf-suite-v1")
        # dead endpoint, yet the submission still goes through (no preflight)
        assert evaluator.start("http://127.0.0.1:1", "m", {})
    finally:
        get_settings.cache_clear()


def test_preflight_passes_for_a_live_endpoint(mock_engine, mock_llmbench):
    client = LLMBenchClient(base_url=mock_llmbench, api_key="", username="u", password="p")
    result = client.preflight(mock_engine, "mock-model")
    assert result["ok"] is True
    assert all(check["status"] == "pass" for check in result["checks"])


def test_busy_platform_raises_evaluator_busy(mock_engine, mock_llmbench, monkeypatch):
    """429 (quota) / 503 (cordon) must not look like a failed config.
    Uses a LIVE endpoint so preflight passes and submit is what rejects."""
    monkeypatch.setenv("MOCK_LLMBENCH_BUSY", "429")
    client = LLMBenchClient(base_url=mock_llmbench, api_key="llmb_testkey")
    evaluator = LLMBenchEvaluator(client=client, benchmark_slug="perf-suite-v1")
    with pytest.raises(EvaluatorBusy, match="429"):
        evaluator.start(mock_engine, "mock-model", {})


def test_a_redline_breach_is_not_a_pass(mock_engine, mock_llmbench, monkeypatch):
    """LLMBench sets status=done whenever the modules RAN; the verdict lives in
    `passed`. Reading only status would let a service that fails its own
    acceptance criteria green-light the canary and top the leaderboard."""
    monkeypatch.setenv("MOCK_LLMBENCH_NOT_PASSED", "1")
    client = LLMBenchClient(base_url=mock_llmbench, api_key="llmb_testkey")
    evaluator = LLMBenchEvaluator(client=client, benchmark_slug="perf-suite-v1")

    ref = evaluator.start(mock_engine, "mock-model", {})
    deadline = time.time() + 20
    outcome = evaluator.poll(ref)
    while outcome.status == EvalStatus.RUNNING and time.time() < deadline:
        time.sleep(0.3)
        outcome = evaluator.poll(ref)

    assert outcome.status == EvalStatus.FAILED
    assert "did not pass" in outcome.error
    assert "perf_mock" in outcome.error  # names the offending module
    assert outcome.metrics, "metrics are still recorded for a failed verdict"


def test_a_failed_module_with_no_error_text_does_not_crash():
    """Live failure: a module failed a threshold with error="" — and
    `"".splitlines()[0]` raised IndexError, turning a correctly-detected
    benchmark failure into an unexplained supervisor crash."""
    from app.evaluation.llmbench import _failed_modules

    submission = {
        "runs": [
            {"module_name": "perf_guidellm_sweep", "passed": False, "error": "", "score": 0.0},
            {"module_name": "whitespace_only", "passed": False, "error": "   \n  "},
            {"module_name": "with_text", "passed": False, "error": "redline breached\ndetail"},
        ]
    }
    described = _failed_modules(submission)
    assert "perf_guidellm_sweep (score 0.0)" in described  # falls back to the score
    assert "whitespace_only" in described
    assert "with_text (redline breached)" in described


def test_llmbench_mock_fails_on_unreachable_endpoint(mock_llmbench, monkeypatch):
    """The post-submission failure path (preflight off, as if it were skipped)."""
    monkeypatch.setenv("AUTOTUNE_LLMBENCH_PREFLIGHT", "false")
    get_settings.cache_clear()
    client = LLMBenchClient(base_url=mock_llmbench, api_key="llmb_testkey")
    evaluator = LLMBenchEvaluator(client=client, benchmark_slug="perf-suite-v1")

    ref = evaluator.start("http://127.0.0.1:1", "nope", {})  # nothing listens there
    deadline = time.monotonic() + 20
    outcome = evaluator.poll(ref)
    while outcome.status == EvalStatus.RUNNING and time.monotonic() < deadline:
        time.sleep(0.3)
        outcome = evaluator.poll(ref)

    assert outcome.status == EvalStatus.FAILED
    assert "probe failed" in outcome.error
    get_settings.cache_clear()


def test_mock_engine_oom_mode_exits_with_oom_log():
    env = {**os.environ, "PYTHONPATH": str(MOCK_ENGINE_DIR)}
    process = subprocess.run(
        [
            sys.executable, "-m", "sglang.launch_server",
            "--port", str(_free_port()),
            "--mock-fail", "oom",
            "--mock-startup-seconds", "0.1",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert process.returncode == 1
    from app.control.launch.ssh_docker import classify_failure

    assert classify_failure(process.stderr) == "oom"


def test_docker_gpu_mode_none_omits_gpu_flags(monkeypatch):
    from app.control.launch import LaunchSpec, MachineInfo
    from app.control.launch.ssh_docker import render_docker_command
    from app.core.config import get_settings

    monkeypatch.setenv("AUTOTUNE_DOCKER_GPU_MODE", "none")
    get_settings.cache_clear()
    try:
        spec = LaunchSpec(
            run_id=1,
            machine=MachineInfo(name="dev", host="127.0.0.1", gpu_count=0),
            engine="sglang",
            image="autotune-mock-engine:latest",
            model_path="/tmp/fake-model",
            served_model_name="mock-model",
        )
        text = " ".join(render_docker_command(spec))
        assert "--gpus" not in text
        assert "--runtime=nvidia" not in text
    finally:
        get_settings.cache_clear()
