"""Retries on transient LLMBench faults.

A flaky intranet answers with gateway timeouts (502/504), dropped connections
and DNS failures that have nothing to do with the config under test. Every call
to LLMBench should ride those out with a generous backoff, and — when a fault
outlasts the retries — defer the run (EvaluatorBusy) rather than fail it. The
one thing that must NOT change: a real status the caller interprets (a 409 from
the dataset builder, backpressure from a busy platform) is not retried away.
"""

import httpx
import pytest

from app.evaluation import EvalStatus, EvaluatorBusy, LLMBenchClient, LLMBenchEvaluator


def make_client(handler, attempts=4):
    """A client whose HTTP goes through `handler` and whose backoff never sleeps."""
    client = LLMBenchClient(
        base_url="http://t", api_key="llmb_k",
        transport=httpx.MockTransport(handler), sleep=lambda _seconds: None,
    )
    client._max_attempts = attempts
    return client


class Handler:
    """Fails the first `failures` calls, then serves `ok`. A negative count
    fails forever. Counts every call so a test can assert the retry budget."""

    def __init__(self, failures, fault, ok=None):
        self.failures = failures
        self.fault = fault  # (request) -> Response, or raises
        self.ok = ok or (lambda r: httpx.Response(200, json={"status": "running"}))
        self.calls = 0

    def __call__(self, request):
        self.calls += 1
        if self.failures < 0 or self.calls <= self.failures:
            return self.fault(request)
        return self.ok(request)


def gw504(request):
    return httpx.Response(504, text="gateway time-out")


def dns_fail(request):
    raise httpx.ConnectError("No address associated with hostname", request=request)


def done(request):
    return httpx.Response(200, json={"status": "done", "passed": True, "runs": []})


def submitted(request):
    return httpx.Response(202, json={"id": 777})


# -- transient faults are ridden out ------------------------------------------


def test_poll_rides_out_transient_gateway_errors():
    handler = Handler(failures=2, fault=gw504, ok=done)
    outcome = LLMBenchEvaluator(client=make_client(handler), benchmark_slug="b").poll("42")
    assert outcome.status == EvalStatus.PASSED
    assert handler.calls == 3  # two 504s ridden out, third succeeded


def test_transport_errors_are_retried_then_succeed():
    handler = Handler(failures=2, fault=dns_fail)
    assert make_client(handler).get_submission("1")["status"] == "running"
    assert handler.calls == 3


def test_submit_succeeds_after_a_blip():
    handler = Handler(failures=3, fault=gw504, ok=submitted)
    assert make_client(handler, attempts=5).submit("b", "http://e", "m") == "777"
    assert handler.calls == 4


# -- a fault that outlasts the retries defers, never fails ---------------------


def test_submit_defers_as_busy_when_gateway_stays_down():
    handler = Handler(failures=-1, fault=gw504)
    with pytest.raises(EvaluatorBusy):
        make_client(handler, attempts=4).submit("b", "http://e", "m")
    assert handler.calls == 4  # exhausted the whole budget before conceding


def test_submit_defers_as_busy_when_unreachable():
    handler = Handler(failures=-1, fault=dns_fail)
    with pytest.raises(EvaluatorBusy):
        make_client(handler, attempts=3).submit("b", "http://e", "m")
    assert handler.calls == 3


def test_poll_stays_running_when_gateway_stays_down():
    """A persistent outage keeps the run RUNNING (retried next tick), never
    marks it failed — the bug that killed two good configs on a 504."""
    handler = Handler(failures=-1, fault=gw504)
    evaluator = LLMBenchEvaluator(client=make_client(handler, attempts=3), benchmark_slug="b")
    outcome = evaluator.poll("7")
    assert outcome.status == EvalStatus.RUNNING
    assert handler.calls == 3


# -- what must NOT be retried away --------------------------------------------


def test_backpressure_is_not_hammered():
    """429/503 are deliberate backpressure: defer at once, do not spend the
    retry budget hitting a platform that already said 'not now'."""
    handler = Handler(failures=-1, fault=lambda r: httpx.Response(503, text="cordon"))
    with pytest.raises(EvaluatorBusy):
        make_client(handler, attempts=5).submit("b", "http://e", "m")
    assert handler.calls == 1


def test_non_retryable_status_is_handed_back_whole():
    """The dataset builder reads statuses itself — a 409 is not an error and
    must not be retried or flattened."""
    handler = Handler(failures=-1, fault=lambda r: httpx.Response(409, json={"detail": "exists"}))
    response = make_client(handler, attempts=5).request("POST", "/datasets/build")
    assert response.status_code == 409
    assert handler.calls == 1
