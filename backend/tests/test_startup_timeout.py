"""The two-clock startup timeout: a cold image pull gets a generous budget, the
model load a tighter one from when its container started.

This is the fix for a k8s node that had never cached a ~20GB engine image — the
first pull ran past the 30-min ready window and the run was failed as wedged when
it was only pulling. The pull phase and the load phase now have separate clocks.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.control.launch.k8s import K8sDriver
from app.control.orchestrator.supervisor import Supervisor
from app.core.config import get_settings
from tests.test_k8s_driver import FakeK8sApi, _clone, _spec

# -- driver: which phase is the pod in ----------------------------------------


def _handle(driver):
    handle, _ = driver.launch(_spec())
    return handle


def test_startup_status_is_pulling_before_a_container_runs():
    settings = _clone(k8s_workload_kind="deployment")
    api = FakeK8sApi(settings)
    driver = K8sDriver(api=api, settings=settings)
    handle = _handle(driver)

    # No pods scheduled yet → still coming up.
    assert driver.startup_status(handle)["phase"] == "pulling"

    # Pod exists but its container is still waiting (ContainerCreating / pulling).
    api.pods = [{"status": {"containerStatuses": [
        {"state": {"waiting": {"reason": "ContainerCreating"}}}
    ]}}]
    assert driver.startup_status(handle)["phase"] == "pulling"


def test_startup_status_is_running_once_a_container_started():
    settings = _clone(k8s_workload_kind="deployment")
    api = FakeK8sApi(settings)
    driver = K8sDriver(api=api, settings=settings)
    handle = _handle(driver)
    api.pods = [{"status": {"containerStatuses": [
        {"state": {"running": {"startedAt": "2026-08-17T06:15:00Z"}}}
    ]}}]

    status = driver.startup_status(handle)
    assert status["phase"] == "running"
    assert status["running_since"] == "2026-08-17T06:15:00Z"


# -- supervisor: the two budgets ----------------------------------------------


def _sup(pull=45, ready=30):
    sup = Supervisor(
        session_factory=None,
        health_evaluator=SimpleNamespace(),
        bench_evaluator=SimpleNamespace(),
    )
    sup.settings = get_settings().model_copy(
        update={"image_pull_timeout_minutes": pull, "ready_timeout_minutes": ready}
    )
    return sup


def _run(started_min_ago: int):
    return SimpleNamespace(started_at=datetime.now(UTC) - timedelta(minutes=started_min_ago))


def _driver_with(status: dict):
    return SimpleNamespace(startup_status=lambda handle: status)


def _running_since(min_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=min_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_a_slow_pull_is_not_failed_until_the_pull_budget():
    sup = _sup(pull=45, ready=30)
    pulling = _driver_with({"phase": "pulling", "running_since": None})
    # 35 min pulling: past the old 30-min ready timeout, but inside the pull budget.
    assert sup._startup_timed_out(_run(35), pulling, None)[0] is False
    # 50 min pulling: past the pull budget → failed, and it says why.
    timed_out, why, waiting = sup._startup_timed_out(_run(50), pulling, None)
    assert timed_out and "image pull" in why and waiting == ""


def test_the_model_load_clock_starts_when_the_container_does():
    sup = _sup(pull=45, ready=30)
    # Pull took 35 min, container has been running 10 min: load budget is fresh.
    running = _driver_with({"phase": "running", "running_since": _running_since(10)})
    assert sup._startup_timed_out(_run(45), running, None)[0] is False
    # Container running 40 min without becoming ready → wedged load, failed.
    stuck = _driver_with({"phase": "running", "running_since": _running_since(40)})
    timed_out, why, waiting = sup._startup_timed_out(_run(75), stuck, None)
    assert timed_out and "container started" in why and waiting == ""


def test_an_unknown_substrate_keeps_the_single_ready_timeout():
    sup = _sup(pull=45, ready=30)
    unknown = _driver_with({"phase": "unknown", "running_since": None})
    assert sup._startup_timed_out(_run(20), unknown, None)[0] is False
    assert sup._startup_timed_out(_run(40), unknown, None)[0] is True


def test_a_driver_that_raises_falls_back_to_the_single_timeout():
    sup = _sup(pull=45, ready=30)

    def _boom(handle):
        raise RuntimeError("cannot read pods")

    driver = SimpleNamespace(startup_status=_boom)
    # Treated as 'unknown' → the plain ready timeout, never an escaped crash.
    assert sup._startup_timed_out(_run(20), driver, None)[0] is False
    assert sup._startup_timed_out(_run(40), driver, None)[0] is True


def _queued_driver(reason: tuple[str, str]):
    """A cluster substrate: still 'pulling' (no container), with a reason."""
    return SimpleNamespace(
        startup_status=lambda handle: {"phase": "pulling", "running_since": None},
        failure_reason=lambda handle: reason,
    )


def test_a_queued_run_is_never_failed_for_waiting():
    """Leasing a k8s pool buys the right to ASK for a schedule. A pod the
    cluster has not placed is queued, not late — production holding the cards
    for hours must not cost the campaign a candidate."""
    sup = _sup(pull=45, ready=30)
    busy = _queued_driver(("unschedulable", "0/40 nodes available: insufficient nvidia.com/gpu"))
    # Well past every budget, and still not a failure.
    for minutes in (50, 240, 60 * 12):
        timed_out, why, waiting = sup._startup_timed_out(_run(minutes), busy, None)
        assert timed_out is False, f"queued run failed after {minutes} min"
        assert why == ""
        assert "insufficient nvidia.com/gpu" in waiting


def test_placement_that_can_never_succeed_still_fails_fast():
    """The other half of the bargain: waiting forever is only safe because a pod
    no node could ever take is a different class, and fails at the budget rather
    than sitting politely until the lease expires."""
    sup = _sup(pull=45, ready=30)
    hopeless = _queued_driver(
        ("unplaceable", "no cluster node matches this machine's node selector (gpu=typo)")
    )
    assert sup._startup_timed_out(_run(20), hopeless, None)[0] is False  # inside the budget
    timed_out, why, waiting = sup._startup_timed_out(_run(50), hopeless, None)
    assert timed_out is True
    assert "node selector" in why
    assert waiting == ""
