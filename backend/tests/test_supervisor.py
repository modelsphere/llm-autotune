"""End-to-end state machine test: fake driver + fake evaluators over sqlite.

Verifies the supervisor drives a run pending → … → succeeded, releases the
machine, records results, and that a crashing container is classified."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.control.launch.base import (
    DeploymentDriver,
    DeploymentHandle,
    DeploymentState,
    LaunchSpec,
)
from app.control.orchestrator.supervisor import Supervisor
from app.control.search import CandidateConfig
from app.db.base import Base
from app.db.models import (
    TERMINAL_RUN_STATES,
    Baseline,
    BaselineStatus,
    Campaign,
    CampaignStatus,
    Candidate,
    CandidateStatus,
    Event,
    LeaseState,
    Machine,
    MachineState,
    Result,
    Run,
    RunKind,
    RunStatus,
    User,
)
from app.evaluation.base import EvalOutcome, EvalStatus, Evaluator


class FakeDriver(DeploymentDriver):
    name = "fake"

    def __init__(self, crash: bool = False, instant_ready: bool = False):
        self.crash = crash
        self.instant_ready = instant_ready
        self.exit_code: int | None = None
        self.oom_killed = False
        self.launched: list[str] = []
        self.torn_down: list[str] = []
        # A torn-down container is gone: attach stops finding it and state
        # reports GONE, so the teardown-confirmation janitor can see cleanup
        # complete (and _finish's fast path release the machine at once).
        self._gone: set[str] = set()
        self.cleared: list[str] = []
        self.restored: list[str] = []
        self._ready: set[str] = set()

    def launch(self, spec: LaunchSpec):
        self._gone.discard(spec.container_name)
        self.launched.append(spec.container_name)
        handle = DeploymentHandle(
            driver=self.name,
            container_name=spec.container_name,
            machine=spec.machine,
            endpoint_url=spec.endpoint_url,
        )
        return handle, f"fake-launch {spec.container_name}"

    def state(self, handle: DeploymentHandle) -> DeploymentState:
        if handle.container_name in self._gone:
            return DeploymentState.GONE
        if self.crash:
            return DeploymentState.CRASHED
        if self.instant_ready or handle.container_name in self._ready:
            return DeploymentState.READY
        self._ready.add(handle.container_name)  # ready on second poll
        return DeploymentState.STARTING

    def logs(self, handle: DeploymentHandle, tail: int = 200) -> str:
        return "torch.cuda.OutOfMemoryError: CUDA out of memory" if self.crash else ""

    def teardown(self, handle: DeploymentHandle) -> None:
        self.torn_down.append(handle.container_name)
        self._gone.add(handle.container_name)

    def attach(self, spec: LaunchSpec):
        if spec.container_name not in self.launched or spec.container_name in self._gone:
            return None
        return DeploymentHandle(
            driver=self.name,
            container_name=spec.container_name,
            machine=spec.machine,
            endpoint_url=spec.endpoint_url,
        )

    def exit_info(self, handle: DeploymentHandle):
        return (self.exit_code, self.oom_killed)

    def capture_baseline(self, machine) -> dict:
        return {"services": [{"container": "prod-svc", "endpoint_url": "http://x:8050"}]}

    def clear_baseline(self, machine, baseline) -> list[str]:
        self.cleared.append(machine.name)
        return ["prod-svc"]

    def restore_baseline(self, machine, baseline) -> list[str]:
        self.restored.append(machine.name)
        return ["prod-svc"]

    def verify_baseline(self, machine, baseline) -> list[dict]:
        return [{"port": "8050", "ok": True, "detail": "matches capture"}]


class PassEvaluator(Evaluator):
    name = "fake-eval"

    def __init__(self, polls_until_done: int = 0):
        self.polls_until_done = polls_until_done
        self._polls = 0

    def start(self, endpoint_url, served_model_name, context) -> str:
        return "ref-1"

    def poll(self, external_ref) -> EvalOutcome:
        self._polls += 1
        if self._polls <= self.polls_until_done:
            return EvalOutcome(status=EvalStatus.RUNNING)
        return EvalOutcome(status=EvalStatus.PASSED, metrics={"score_total": 123.0})


class FailingEvaluator(Evaluator):
    name = "failing"

    def start(self, endpoint_url, served_model_name, context) -> str:
        return "ref-fail"

    def poll(self, external_ref) -> EvalOutcome:
        return EvalOutcome(status=EvalStatus.FAILED, error="service is sick")


class BenchFailedEvaluator(Evaluator):
    """The benchmark ran and the service failed it: passed:False, the shape
    _bench_failure_class reads as a verdict on the config rather than flaky
    infra. Models run 216 — the engine crashed mid-replay and uptime collapsed."""

    name = "bench-failed"

    def start(self, endpoint_url, served_model_name, context) -> str:
        return "ref-bench-fail"

    def poll(self, external_ref) -> EvalOutcome:
        return EvalOutcome(
            status=EvalStatus.FAILED,
            error="benchmark ran but did not pass: replay_prod (uptime 0.06 < 0.99)",
            metrics={"replay_prod.uptime": 0.06},
            raw={"passed": False, "runs": [{"module_name": "replay_prod", "passed": False}]},
        )


def make_supervisor(crash: bool = False, instant_ready: bool = False, bench=None):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add(User(id=1, username="u", password_hash="x"))
        session.add(
            Machine(
                id=1, name="gpu-01", host="10.0.0.1",
                state=MachineState.AVAILABLE.value, gpu_count=8,
                # Production already handed over and cleared: these tests are
                # about run states, not the baseline lifecycle, which has its
                # own tests below.
                baseline_status=BaselineStatus.CLEARED.value,
            )
        )
        session.add(
            Campaign(
                id=1, owner_id=1, name="c", engine="sglang", image="img",
                model_path="/models/m", served_model_name="m",
                search_space={"grid": {"tp_size": [2]}},
                status=CampaignStatus.ACTIVE.value,
            )
        )
        session.commit()
    supervisor = Supervisor(
        session_factory=factory,
        driver_name="ssh_docker",  # replaced below
        health_evaluator=PassEvaluator(),
        bench_evaluator=bench or PassEvaluator(polls_until_done=1),
    )
    supervisor.driver = FakeDriver(crash=crash, instant_ready=instant_ready)
    return supervisor, factory


def _run_status(factory) -> str | None:
    with factory() as session:
        run = session.scalars(select(Run)).first()
        return run.status if run else None


def test_happy_path_to_succeeded():
    supervisor, factory = make_supervisor()

    supervisor.tick()  # plan + schedule + launch (pending -> launching)
    assert _run_status(factory) == RunStatus.LAUNCHING.value

    supervisor.tick()  # starting -> waiting_ready
    assert _run_status(factory) == RunStatus.WAITING_READY.value

    supervisor.tick()  # ready -> health_check
    assert _run_status(factory) == RunStatus.HEALTH_CHECK.value

    supervisor.tick()  # health passes -> benching
    assert _run_status(factory) == RunStatus.BENCHING.value

    supervisor.tick()  # bench poll 1: still running
    assert _run_status(factory) == RunStatus.BENCHING.value

    supervisor.tick()  # bench poll 2: passed -> succeeded + teardown + release
    assert _run_status(factory) == RunStatus.SUCCEEDED.value
    assert supervisor.driver.torn_down == ["autotune-run-1"]

    with factory() as session:
        machine = session.get(Machine, 1)
        assert machine.state == MachineState.AVAILABLE.value
        results = session.scalars(select(Result)).all()
        assert {r.source for r in results} == {"health", "llmbench"}
        campaign = session.get(Campaign, 1)
        # single-point grid exhausted and no live runs -> campaign done
        supervisor.tick()
        session.refresh(campaign)
        assert campaign.status == CampaignStatus.DONE.value


def test_instantly_ready_service_skips_waiting_state():
    # Regression: a service READY on the first poll (no-load mock engine)
    # crashed the supervisor with "illegal transition launching -> health_check".
    supervisor, factory = make_supervisor(instant_ready=True)

    supervisor.tick()  # schedule + launch
    assert _run_status(factory) == RunStatus.LAUNCHING.value

    supervisor.tick()  # ready immediately -> health_check (skips waiting_ready)
    assert _run_status(factory) == RunStatus.HEALTH_CHECK.value

    supervisor.tick()  # health passes -> benching
    assert _run_status(factory) == RunStatus.BENCHING.value


def test_baseline_canary_runs_first_and_never_tears_down_production():
    """With a captured baseline, the first run measures production in place:
    no container is launched, and teardown must NOT touch it."""
    supervisor, factory = make_supervisor()
    with factory() as session:
        machine = session.get(Machine, 1)
        machine.baseline = {
            "services": [
                {
                    "container": "sglang-prod-p8050",
                    "endpoint_url": "http://10.0.0.1:8050",
                    "served_model_name": "glm-5",
                }
            ]
        }
        machine.baseline_status = BaselineStatus.CAPTURED.value
        session.commit()

    supervisor.tick()  # schedules the canary, health passes -> benching
    with factory() as session:
        run = session.scalars(select(Run)).first()
        assert run.kind == RunKind.BASELINE.value
        assert run.endpoint_url == "http://10.0.0.1:8050"
        assert run.status == RunStatus.BENCHING.value
    assert supervisor.driver.launched == [], "a baseline run must not launch anything"

    supervisor.tick()  # bench still running
    supervisor.tick()  # bench done -> succeeded
    with factory() as session:
        run = session.scalars(select(Run)).first()
        assert run.status == RunStatus.SUCCEEDED.value
        assert session.get(Machine, 1).state == MachineState.AVAILABLE.value
    assert supervisor.driver.torn_down == [], "production must survive the canary"


def test_experiments_wait_until_production_is_cleared():
    """The scheduler gate in isolation: a captured-but-not-cleared machine
    still hosts production, and an experiment there would fight it for GPUs.
    (Auto-lifecycle is off here — it would legitimately clear in the same
    tick; that path is covered by its own tests.)"""
    supervisor, factory = make_supervisor()
    supervisor.settings = supervisor.settings.model_copy(
        update={"auto_baseline_lifecycle": False}
    )
    with factory() as session:
        machine = session.get(Machine, 1)
        machine.baseline = {"services": [{"container": "p", "endpoint_url": "http://10.0.0.1:8050"}]}
        machine.baseline_status = BaselineStatus.CAPTURED.value
        campaign = session.get(Campaign, 1)
        campaign.run_baseline_canary = False  # no canary; only experiments pending
        session.commit()

    supervisor.tick()
    with factory() as session:
        assert session.scalars(select(Run)).first() is None, "must not run on a live-prod machine"

    with factory() as session:  # once cleared, experiments proceed
        session.get(Machine, 1).baseline_status = BaselineStatus.CLEARED.value
        session.commit()
    supervisor.tick()
    with factory() as session:
        run = session.scalars(select(Run)).first()
        assert run is not None and run.kind == RunKind.EXPERIMENT.value


def _capture_baseline_on(factory, status=BaselineStatus.CAPTURED.value):
    with factory() as session:
        machine = session.get(Machine, 1)
        machine.baseline = {
            "services": [
                {
                    "container": "sglang-prod-p8050",
                    "endpoint_url": "http://10.0.0.1:8050",
                    "served_model_name": "glm-5",
                    "port": "8050",
                    "cards": 2,
                }
            ]
        }
        machine.baseline_status = status
        session.commit()


def test_production_is_cleared_automatically_after_a_passing_canary():
    """An unattended night cannot wait for a human to press Clear."""
    supervisor, factory = make_supervisor()
    _capture_baseline_on(factory)

    supervisor.tick()  # canary scheduled -> health -> benching
    supervisor.tick()  # bench poll 1
    supervisor.tick()  # canary succeeds, machine released
    with factory() as session:
        assert session.scalars(select(Run)).first().status == RunStatus.SUCCEEDED.value

    supervisor.tick()  # lifecycle: canary passed + work waiting -> clear
    with factory() as session:
        assert session.get(Machine, 1).baseline_status == BaselineStatus.CLEARED.value
        event = session.scalars(
            select(Event).where(Event.kind == "baseline_cleared_auto")
        ).first()
        assert event is not None


def test_a_stale_canary_from_another_campaign_does_not_authorize_clearing():
    """Regression: the lifecycle accepted ANY passed canary on the machine, so
    an hour-old one from a finished campaign cleared production before this
    campaign's canary had even been scheduled — the night ran with no
    baseline."""
    supervisor, factory = make_supervisor()
    _capture_baseline_on(factory)
    with factory() as session:
        old_campaign = Campaign(
            id=2, owner_id=1, name="yesterday", engine="sglang", image="img",
            model_path="/m", served_model_name="m", search_space={},
            status=CampaignStatus.DONE.value,
        )
        session.add(old_campaign)
        session.flush()
        candidate = Candidate(campaign_id=2, config={}, config_hash="old")
        session.add(candidate)
        session.flush()
        session.add(
            Run(campaign_id=2, candidate_id=candidate.id, machine_id=1,
                kind=RunKind.BASELINE.value, status=RunStatus.SUCCEEDED.value)
        )
        session.commit()

    supervisor.tick()
    with factory() as session:
        assert session.get(Machine, 1).baseline_status == BaselineStatus.CAPTURED.value, (
            "another campaign's canary must not authorize tearing production down"
        )
        this_campaigns_canary = session.scalars(
            select(Run).where(Run.campaign_id == 1, Run.kind == RunKind.BASELINE.value)
        ).first()
        assert this_campaigns_canary is not None, "its own canary should be scheduled"


def test_a_failed_canary_leaves_production_alone():
    """A suspect machine is precisely the one not to tear down."""
    supervisor, factory = make_supervisor()
    supervisor.health = FailingEvaluator()
    _capture_baseline_on(factory)

    supervisor.tick()  # canary scheduled, health fails
    supervisor.tick()  # lifecycle would clear — must not
    with factory() as session:
        run = session.scalars(select(Run)).first()
        assert run.status == RunStatus.FAILED.value
        assert session.get(Machine, 1).baseline_status == BaselineStatus.CAPTURED.value
    assert supervisor.driver.cleared == [], "production must survive a failed canary"


def test_production_is_restored_when_the_window_closes():
    supervisor, factory = make_supervisor()
    supervisor.settings = supervisor.settings.model_copy(
        update={"auto_restore_production": True}  # put-back is opt-in
    )
    _capture_baseline_on(factory, status=BaselineStatus.CLEARED.value)
    with factory() as session:
        campaign = session.get(Campaign, 1)
        # window already over
        campaign.window_end = datetime.now(UTC) - timedelta(minutes=1)
        session.commit()

    supervisor.tick()
    with factory() as session:
        assert session.get(Machine, 1).baseline_status == BaselineStatus.RESTORED.value


def test_window_close_leaves_production_down_by_default_for_the_admin():
    """Auto-restore is off by default: when the window closes we do NOT restart
    production (the admin owns that). The box is handed back with production as
    we left it, and the machine no longer claims a baseline it isn't holding."""
    supervisor, factory = make_supervisor()  # auto_restore_production defaults off
    _capture_baseline_on(factory, status=BaselineStatus.CLEARED.value)
    with factory() as session:
        machine = session.get(Machine, 1)
        machine.lease_state = LeaseState.ACTIVE.value
        machine.lease_due_at = datetime.now(UTC) - timedelta(minutes=1)  # lease overdue
        session.get(Campaign, 1).window_end = datetime.now(UTC) - timedelta(minutes=1)
        session.commit()

    supervisor.tick()

    with factory() as session:
        machine = session.get(Machine, 1)
        assert supervisor.driver.restored == [], "must not relaunch production"
        assert machine.baseline_status != BaselineStatus.RESTORED.value, (
            "we did not restore, so must not claim RESTORED"
        )
        assert session.scalars(
            select(Event).where(Event.kind == "production_left_down")
        ).first() is not None


def test_no_window_means_no_automatic_restore():
    """Without a declared hand-back time the session is supervised — never
    yank the machine out from under someone still iterating."""
    supervisor, factory = make_supervisor()
    _capture_baseline_on(factory, status=BaselineStatus.CLEARED.value)

    supervisor.tick()
    with factory() as session:
        assert session.get(Machine, 1).baseline_status == BaselineStatus.CLEARED.value


def test_capture_keeps_a_prior_baseline_when_it_transiently_sees_nothing():
    """Prod-priority: an empty `docker ps` on a box where we captured
    production before means the service is restarting, not that the box became
    ours. Overwriting the capture with the empty reading would strand
    production — nothing left to restore it with — and mark the machine free to
    run our containers over. The prior capture is kept and the machine stays put
    until production is seen again."""
    supervisor, factory = make_supervisor()
    supervisor.driver.capture_baseline = lambda machine: {"services": []}
    with factory() as session:
        machine = session.get(Machine, 1)
        machine.baseline = {
            "services": [{"container": "prod-svc", "endpoint_url": "http://x:8050"}]
        }
        machine.baseline_status = BaselineStatus.RESTORED.value
        machine.state = MachineState.AVAILABLE.value
        session.commit()

    supervisor.tick()

    with factory() as session:
        machine = session.get(Machine, 1)
        assert machine.baseline_status == BaselineStatus.RESTORED.value, (
            "an empty capture must not mark a box with known production as free"
        )
        assert (machine.baseline or {}).get("services"), "prior baseline must survive"
        assert supervisor.driver.cleared == [], "nothing may be cleared off a kept baseline"
        assert session.scalars(
            select(Event).where(Event.kind == "baseline_capture_kept_prior")
        ).first() is not None


def test_a_paused_campaign_freezes_its_expired_lease():
    """Seen live: an operator hit Pause meaning to end a lease.
    The lease kept its own clock, expired, and auto-drained — tearing production
    back down and relaunching it over a service the operator had restored by
    hand. Paused means frozen: an overdue lease held only by a paused campaign is
    left untouched — not drained, production not restored on its own."""
    supervisor, factory = make_supervisor()
    _capture_baseline_on(factory, status=BaselineStatus.CLEARED.value)
    with factory() as session:
        session.get(Campaign, 1).status = CampaignStatus.PAUSED.value
        machine = session.get(Machine, 1)
        machine.lease_state = LeaseState.ACTIVE.value
        machine.lease_due_at = datetime.now(UTC) - timedelta(minutes=1)  # overdue
        session.commit()

    supervisor.tick()

    with factory() as session:
        machine = session.get(Machine, 1)
        assert machine.lease_state == LeaseState.ACTIVE.value, "a frozen lease must not drain"
        assert machine.baseline_status == BaselineStatus.CLEARED.value, "production left as-is"
        assert not session.scalars(
            select(Event).where(Event.kind == "lease_end_requested")
        ).first(), "no auto hand-back while frozen"


def test_infrastructure_failures_give_the_candidate_another_chance():
    """An ssh timeout says nothing about the config. Losing a candidate to one
    means the night silently tests less than it was asked to."""
    supervisor, factory = make_supervisor()

    def boom(spec):
        raise RuntimeError(f"ssh to {spec.machine.host} timed out after 60s")

    supervisor.driver.launch = boom
    supervisor.tick()  # schedule + launch explodes

    with factory() as session:
        run = session.scalars(select(Run)).first()
        assert run.status == RunStatus.FAILED.value
        assert run.failure_class == "ssh_timeout"
        candidate = session.get(Candidate, run.candidate_id)
        assert candidate.status == CandidateStatus.VALID.value, "must be retryable"
        assert session.scalars(
            select(Event).where(Event.kind == "candidate_requeued")
        ).first() is not None


def test_a_supervisor_crash_explains_itself_on_the_run():
    """Live gap: run 37 showed "unexpected supervisor exception" and no log —
    the traceback existed only in the worker log on the platform host, so
    diagnosing it required ssh. The run itself must say what happened."""
    supervisor, factory = make_supervisor()

    def explode(spec):
        return "".splitlines()[0]  # the actual bug that killed run 37

    supervisor.driver.launch = explode
    supervisor.tick()

    with factory() as session:
        run = session.scalars(select(Run)).first()
        assert run.failure_class == "supervisor_error"
        assert "IndexError" in run.error, f"needs the exception type: {run.error}"
        assert "test_supervisor.py:" in run.error, "and where it came from"


def test_our_own_crashes_are_not_retried():
    """supervisor_error means our code raised something unanticipated — nearly
    always deterministic. Retrying costs a model load and a benchmark per
    attempt for no chance of a different answer."""
    supervisor, factory = make_supervisor()

    def explode(spec):
        raise ValueError("a bug in our code, not the machine's fault")

    supervisor.driver.launch = explode
    supervisor.tick()

    with factory() as session:
        run = session.scalars(select(Run)).first()
        assert run.failure_class == "supervisor_error"
        assert session.get(Candidate, run.candidate_id).status == (
            CandidateStatus.EXHAUSTED.value
        ), "a deterministic crash must not consume the night in retries"


def test_a_configs_own_failure_is_not_retried():
    """OOM is a verdict on the config; retrying it just wastes the night."""
    supervisor, factory = make_supervisor(crash=True)  # logs report CUDA OOM
    supervisor.tick()  # launch
    supervisor.tick()  # crashed -> failed

    with factory() as session:
        run = session.scalars(select(Run)).first()
        assert run.failure_class == "oom"
        candidate = session.get(Candidate, run.candidate_id)
        assert candidate.status == CandidateStatus.EXHAUSTED.value


def test_retries_are_bounded():
    supervisor, factory = make_supervisor()
    supervisor.driver.launch = lambda spec: (_ for _ in ()).throw(
        RuntimeError("ssh timed out after 60s")
    )
    for _ in range(6):
        supervisor.tick()

    with factory() as session:
        runs = session.scalars(select(Run)).all()
        assert len(runs) == 3, f"bounded at 3 attempts, got {len(runs)}"
        assert session.scalars(
            select(Event).where(Event.kind == "candidate_given_up")
        ).first() is not None


def test_campaign_only_uses_its_pinned_machines():
    """Regression: a GPU campaign was scheduled onto an unrelated free box."""
    supervisor, factory = make_supervisor()
    with factory() as session:
        session.add(
            Machine(
                id=2, name="other-box", host="10.0.0.2",
                state=MachineState.AVAILABLE.value, gpu_count=8,
            )
        )
        session.get(Campaign, 1).machine_names = ["other-box"]
        session.commit()

    supervisor.tick()
    with factory() as session:
        run = session.scalars(select(Run)).first()
        assert run.machine_id == 2, "must use the pinned machine, not the lowest id"
        assert session.get(Machine, 1).state == MachineState.AVAILABLE.value


def test_a_campaign_that_wants_the_machine_to_itself_waits_for_it():
    """Busy is a live run, not a flag: `reserved` now only means "someone is
    here", which with sharing no longer implies "full"."""
    supervisor, factory = make_supervisor()
    with factory() as session:
        campaign = session.get(Campaign, 1)
        campaign.machine_names = ["gpu-01"]
        campaign.share_machine = False
        campaign.search_space = {"grid": {"tp_size": [2, 4]}}
        session.commit()

    supervisor.tick()  # one run starts and takes the machine
    supervisor.tick()  # the second must not join it

    with factory() as session:
        live = session.scalars(
            select(Run).where(Run.status.not_in([s.value for s in TERMINAL_RUN_STATES]))
        ).all()
        assert len(live) == 1


def test_crash_is_classified_and_machine_released():
    supervisor, factory = make_supervisor(crash=True)

    supervisor.tick()  # launch
    supervisor.tick()  # crashed -> captured + failed

    with factory() as session:
        run = session.scalars(select(Run)).first()
        assert run.status == RunStatus.FAILED.value
        assert run.failure_class == "oom"
        machine = session.get(Machine, 1)
        assert machine.state == MachineState.AVAILABLE.value


def test_a_benchmark_failure_captures_the_engine_log(tmp_path, monkeypatch):
    """A replay can fail because the served engine died under load (run 216:
    6.5% uptime). The OOM traceback lives only in the container's stderr, and
    _fail tears the container down — so the log must be grabbed at the verdict,
    the way a launch-time crash already is."""
    supervisor, factory = make_supervisor(
        instant_ready=True, bench=BenchFailedEvaluator()
    )
    monkeypatch.setattr(supervisor.settings, "run_log_dir", str(tmp_path))
    supervisor.driver.logs = lambda handle, tail=400: (
        "[rank0] torch.cuda.OutOfMemoryError: CUDA out of memory. "
        "Tried to allocate 2.00 GiB\nengine process exited"
    )

    for _ in range(6):
        supervisor.tick()
        if _run_status(factory) == RunStatus.FAILED.value:
            break

    with factory() as session:
        run = session.scalars(select(Run)).first()
        assert run.status == RunStatus.FAILED.value
        # The verdict is preserved — capturing the log must not reclassify it.
        assert run.failure_class == "benchmark_not_passed"
        assert run.log_path, "the engine log was saved before teardown"
        with open(run.log_path, encoding="utf-8") as f:
            assert "CUDA out of memory" in f.read()
    # And the container was still torn down afterwards.
    assert supervisor.driver.torn_down == [f"autotune-run-{run.id}"]


def test_a_benchmark_failure_without_a_readable_log_leaves_log_path_unset(monkeypatch):
    """A redline breach on a healthy engine, or a container already gone, yields
    no log — log_path stays empty rather than pointing at an empty file, and the
    verdict lands exactly the same."""
    supervisor, factory = make_supervisor(
        instant_ready=True, bench=BenchFailedEvaluator()
    )
    supervisor.driver.logs = lambda handle, tail=400: ""

    for _ in range(6):
        supervisor.tick()
        if _run_status(factory) == RunStatus.FAILED.value:
            break

    with factory() as session:
        run = session.scalars(select(Run)).first()
        assert run.status == RunStatus.FAILED.value
        assert run.failure_class == "benchmark_not_passed"
        assert not run.log_path


def test_external_kill_classified_from_exit_code():
    supervisor, factory = make_supervisor(crash=True)
    supervisor.driver.exit_code = 137  # docker kill / SIGKILL

    def _no_logs(handle, tail=200):
        return ""  # nothing useful in the logs

    supervisor.driver.logs = _no_logs
    supervisor.tick()  # launch
    supervisor.tick()  # crashed, logs empty -> exit-code classification

    with factory() as session:
        run = session.scalars(select(Run)).first()
        assert run.status == RunStatus.FAILED.value
        assert run.failure_class == "killed_externally"
        assert "exit code 137" in run.error


def test_user_stop_request_kills_run_and_releases_machine():
    supervisor, factory = make_supervisor()

    supervisor.tick()  # launch (pending -> launching)
    with factory() as session:
        run = session.scalars(select(Run)).first()
        session.add(Event(actor="alice", kind="stop_requested", run_id=run.id))
        session.commit()

    supervisor.tick()  # stop request processed before anything else

    with factory() as session:
        run = session.scalars(select(Run)).first()
        assert run.status == RunStatus.KILLED.value
        assert run.error == "stopped by user"
        machine = session.get(Machine, 1)
        assert machine.state == MachineState.AVAILABLE.value
    assert supervisor.driver.torn_down == ["autotune-run-1"]


def test_a_restored_machine_is_captured_again_for_the_next_night():
    """The lifecycle drove captured -> cleared -> restored automatically but
    nothing moved a machine back INTO captured, so only the first night was
    unattended: every night after it stalled behind "capture the baseline"."""
    supervisor, factory = make_supervisor()
    with factory() as session:
        machine = session.get(Machine, 1)
        machine.baseline_status = BaselineStatus.RESTORED.value  # last night put production back
        session.commit()

    supervisor.tick()

    with factory() as session:
        machine = session.get(Machine, 1)
        assert machine.baseline_status == BaselineStatus.CAPTURED.value
        assert machine.baseline["services"][0]["container"] == "prod-svc"
        kinds = {e.kind for e in session.scalars(select(Event)).all()}
        assert "baseline_captured_auto" in kinds


def test_capture_promotes_production_to_a_first_class_baseline():
    """A captured production service becomes a baseline row keyed by
    (served_model_name, engine, card_type) — the reusable reference, no longer
    trapped inside the machine's capture blob."""
    supervisor, factory = make_supervisor()
    supervisor.driver.capture_baseline = lambda machine: {"services": [{
        "container": "prod", "endpoint_url": "http://x:8050",
        "served_model_name": "glm-5",
        "command": '["python", "-m", "sglang.launch_server", "--tp", "2", '
                   '"--enable-cache-report", "--port", "8050"]',
        "engine_args": {"tp": "2", "enable_cache_report": True},
    }]}
    with factory() as session:
        machine = session.get(Machine, 1)
        machine.gpu_type = "A100"
        machine.baseline_status = BaselineStatus.RESTORED.value
        session.commit()

    supervisor.tick()

    with factory() as session:
        baseline = session.scalars(select(Baseline)).one()
        assert (baseline.served_model_name, baseline.engine, baseline.card_type) == (
            "glm-5", "sglang", "A100",
        )
        assert baseline.engine_args == {"tp": "2", "enable_cache_report": True}
        assert baseline.source == "capture:gpu-01"


def test_a_hand_set_baseline_is_not_overwritten_by_capture():
    """Capture maintains only what it created; an operator's manual reference
    survives the next hand-over rather than being silently replaced."""
    supervisor, factory = make_supervisor()
    supervisor.driver.capture_baseline = lambda machine: {"services": [{
        "container": "prod", "endpoint_url": "http://x:8050",
        "served_model_name": "glm-5",
        "engine_args": {"tp": "8"},
    }]}
    with factory() as session:
        machine = session.get(Machine, 1)
        machine.gpu_type = "A100"
        machine.baseline_status = BaselineStatus.RESTORED.value
        session.add(Baseline(
            served_model_name="glm-5", engine="sglang", card_type="A100",
            engine_args={"tp": "2"}, source="manual",
        ))
        session.commit()

    supervisor.tick()

    with factory() as session:
        baseline = session.scalars(select(Baseline)).one()
        assert baseline.engine_args == {"tp": "2"}, "the manual reference stands"
        assert baseline.source == "manual"


def test_an_idle_machine_is_measured_free_rather_than_assumed_free():
    """A machine we have never inspected is not the same as an empty one.
    Capture reports nothing running -> it is ours, and now we know it."""
    supervisor, factory = make_supervisor()
    supervisor.driver.capture_baseline = lambda machine: {"services": []}
    with factory() as session:
        session.get(Machine, 1).baseline_status = BaselineStatus.NONE.value
        session.commit()

    supervisor.tick()

    with factory() as session:
        machine = session.get(Machine, 1)
        assert machine.baseline_status == BaselineStatus.CLEARED.value
        kinds = {e.kind for e in session.scalars(select(Event)).all()}
        assert "baseline_capture_found_nothing" in kinds


def test_production_running_on_a_fresh_machine_is_protected_not_ignored():
    """Regression: NONE used to schedule experiments straight onto the machine,
    which would have launched them alongside whatever was already serving."""
    supervisor, factory = make_supervisor()
    with factory() as session:
        session.get(Machine, 1).baseline_status = BaselineStatus.NONE.value
        session.commit()

    supervisor.tick()

    with factory() as session:
        machine = session.get(Machine, 1)
        assert machine.baseline_status == BaselineStatus.CAPTURED.value
        run = session.scalars(select(Run)).first()
        # The first run is the canary against production, not an experiment.
        assert run is not None and run.kind == RunKind.BASELINE.value
        assert supervisor.driver.launched == [], "nothing may be launched yet"


def test_a_machine_nobody_is_waiting_for_is_left_alone():
    """Capture only reads, but reading means sshing into someone's box — do it
    only when a campaign actually needs the machine."""
    supervisor, factory = make_supervisor()
    with factory() as session:
        session.get(Machine, 1).baseline_status = BaselineStatus.RESTORED.value
        session.get(Campaign, 1).status = CampaignStatus.PAUSED.value
        session.commit()

    supervisor.tick()

    with factory() as session:
        assert session.get(Machine, 1).baseline_status == BaselineStatus.RESTORED.value


def test_a_machine_marked_away_is_not_captured():
    """`away` means production hours: it has not been handed over, and the
    hand-over itself stays a human decision."""
    supervisor, factory = make_supervisor()
    with factory() as session:
        machine = session.get(Machine, 1)
        machine.baseline_status = BaselineStatus.RESTORED.value
        machine.state = MachineState.AWAY.value
        session.commit()

    supervisor.tick()

    with factory() as session:
        assert session.get(Machine, 1).baseline_status == BaselineStatus.RESTORED.value


def _request_stop(factory):
    """What POST /runs/{id}/stop records."""
    with factory() as session:
        run = session.scalars(select(Run).order_by(Run.id.desc())).first()
        session.add(Event(actor="u", kind="stop_requested", run_id=run.id, payload={}))
        session.commit()


def test_a_failed_canary_is_retried_but_not_forever():
    """One canary per campaign+machine, ever, deadlocked the campaign: clearing
    production needs a canary that PASSED, and nothing scheduled another one.
    Retry — but production that cannot be measured is a finding, not a loop."""
    supervisor, factory = make_supervisor()
    supervisor.health = FailingEvaluator()
    _capture_baseline_on(factory)

    for _ in range(8):
        supervisor.tick()

    with factory() as session:
        canaries = session.scalars(select(Run).where(Run.kind == RunKind.BASELINE.value)).all()
        assert len(canaries) == 3, "retried up to the bound, then stopped"
        assert all(r.status == RunStatus.FAILED.value for r in canaries)
        assert session.get(Machine, 1).baseline_status == BaselineStatus.CAPTURED.value
    assert supervisor.driver.cleared == [], "production survives every failed canary"


def test_a_canary_stopped_by_hand_is_not_immediately_rescheduled():
    """Stop means stop. Re-scheduling on the next tick would undo the click."""
    supervisor, factory = make_supervisor()
    _capture_baseline_on(factory)
    supervisor.tick()  # canary scheduled

    _request_stop(factory)
    supervisor.tick()  # honours the stop
    supervisor.tick()  # must not start another

    with factory() as session:
        canaries = session.scalars(select(Run).where(Run.kind == RunKind.BASELINE.value)).all()
        assert len(canaries) == 1
        assert canaries[0].status == RunStatus.KILLED.value


def test_restarting_the_campaign_re_runs_a_stopped_canary():
    """The escape hatch from a stopped canary is the Start button — otherwise
    the campaign is stuck behind evidence it can never obtain."""
    supervisor, factory = make_supervisor()
    _capture_baseline_on(factory)
    supervisor.tick()
    _request_stop(factory)
    supervisor.tick()

    with factory() as session:  # what the Start button records
        session.add(
            Event(
                actor="u",
                kind="campaign_status_changed",
                campaign_id=1,
                # Explicit, later timestamp: sqlite's CURRENT_TIMESTAMP has
                # one-second resolution, so "after the run" needs saying.
                ts=datetime.now(UTC) + timedelta(seconds=5),
                payload={"status": CampaignStatus.ACTIVE.value},
            )
        )
        session.commit()
    supervisor.tick()

    with factory() as session:
        canaries = session.scalars(select(Run).where(Run.kind == RunKind.BASELINE.value)).all()
        assert len(canaries) == 2, "a fresh start earns a fresh canary"
        assert canaries[-1].status != RunStatus.KILLED.value


def _grid(factory, values, gpu_count=8, share=True):
    """Replace the fixture's candidates with one per value, widest first."""
    with factory() as session:
        campaign = session.get(Campaign, 1)
        campaign.search_space = {"grid": {"tp_size": values}}
        campaign.share_machine = share
        session.get(Machine, 1).gpu_count = gpu_count
        for row in session.scalars(select(Candidate)).all():
            session.delete(row)
        session.flush()
        for tp in values:
            config = {"tp_size": tp}
            session.add(
                Candidate(
                    campaign_id=campaign.id,
                    config=config,
                    config_hash=CandidateConfig(engine_args=config).hash,
                    status=CandidateStatus.VALID.value,
                )
            )
        session.commit()


def test_a_mixed_width_grid_fills_the_machine_in_one_tick():
    """8 cards running one tp=2 config wastes six of them all night. Widest
    first: tp=4 takes 0-3, then the two tp=2s take 4-5 and 6-7."""
    supervisor, factory = make_supervisor()
    _grid(factory, [4, 2, 2])

    supervisor.tick()

    with factory() as session:
        runs = session.scalars(select(Run).order_by(Run.id)).all()
        assert len(runs) == 3
        cards = [r.gpu_indices for r in runs]
        assert cards[0] == [0, 1, 2, 3], "the widest candidate is placed first"
        assert sorted(sum(cards, [])) == list(range(8)), "every card used exactly once"
        ports = [r.service_port for r in runs]
        assert len(set(ports)) == 3, "co-tenants share a host network namespace"
        assert session.get(Machine, 1).state == MachineState.RESERVED.value


def test_a_candidate_too_wide_for_the_remaining_cards_waits():
    supervisor, factory = make_supervisor()
    _grid(factory, [4, 4, 2], gpu_count=8)

    supervisor.tick()

    with factory() as session:
        runs = session.scalars(select(Run).order_by(Run.id)).all()
        assert [r.gpu_indices for r in runs] == [[0, 1, 2, 3], [4, 5, 6, 7]]
        waiting = session.scalars(
            select(Candidate).where(Candidate.status == CandidateStatus.VALID.value)
        ).all()
        assert len(waiting) == 1, "the tp=2 waits rather than being squeezed in"


def test_sharing_off_means_one_run_owns_the_whole_machine():
    supervisor, factory = make_supervisor()
    _grid(factory, [2, 4], share=False)

    supervisor.tick()

    with factory() as session:
        runs = session.scalars(select(Run)).all()
        assert len(runs) == 1
        # Still pinned to exactly the cards the config asks for: the rest sit
        # idle because that is what turning sharing off means.
        assert runs[0].gpu_indices == [0, 1, 2, 3]


def test_the_machine_is_released_only_when_the_last_co_tenant_leaves():
    """Releasing on the first would let the baseline lifecycle clear or restore
    production underneath a still-running experiment."""
    supervisor, factory = make_supervisor(instant_ready=True)
    _grid(factory, [4, 4])
    supervisor.tick()

    with factory() as session:
        first = session.scalars(select(Run).order_by(Run.id)).first()
        session.add(Event(actor="u", kind="stop_requested", run_id=first.id, payload={}))
        session.commit()
    supervisor.tick()

    with factory() as session:
        assert session.get(Machine, 1).state == MachineState.RESERVED.value, (
            "one co-tenant is still running"
        )


def test_a_cpu_only_box_still_hosts_exactly_one_run():
    """The mock/test host has no cards to divide; sharing must not deadlock it."""
    supervisor, factory = make_supervisor()
    _grid(factory, [1, 1], gpu_count=0)

    supervisor.tick()

    with factory() as session:
        runs = session.scalars(select(Run)).all()
        assert len(runs) == 1
        assert runs[0].gpu_indices == []


def test_a_run_holding_unrecorded_cards_blocks_co_tenants():
    """Unknown must read as "all", not "none". Live on node-24: a run created
    before per-GPU pinning recorded no cards, so the scheduler placed two
    experiments on top of a run that was using the whole machine."""
    supervisor, factory = make_supervisor()
    # 4 cards, so the tp=4 takes the machine and the tp=2 has to wait.
    _grid(factory, [4, 2], gpu_count=4)
    supervisor.tick()

    with factory() as session:
        run = session.scalars(select(Run).order_by(Run.id)).first()
        run.gpu_indices = []  # what a pre-sharing run looks like
        session.commit()
    supervisor.tick()

    with factory() as session:
        live = session.scalars(
            select(Run).where(Run.status.not_in([s.value for s in TERMINAL_RUN_STATES]))
        ).all()
        assert len(live) == 1, "nothing may join a run whose cards are unknown"


def test_a_pre_sharing_run_still_owns_its_campaign_port():
    """Run 47 failed with port_conflict: the co-tenant recorded port 0, so the
    scheduler handed the same 28200 to the next run."""
    supervisor, factory = make_supervisor()
    _grid(factory, [2, 2])
    supervisor.tick()

    with factory() as session:
        first = session.scalars(select(Run).order_by(Run.id)).first()
        first.service_port = 0  # pre-sharing run, listening on the campaign port
        session.commit()
        campaign = session.get(Campaign, 1)
        live = [session.scalars(select(Run).order_by(Run.id)).first()]
        assert supervisor._free_port(campaign, live) != campaign.service_port


class WedgedPodDriver(FakeDriver):
    """A workload that never starts and leaves no log — but whose substrate
    knows exactly why (a pod Pending on a cordoned node)."""

    def logs(self, handle, tail: int = 200) -> str:
        return ""

    def failure_reason(self, handle):
        return ("unschedulable", "0/19 nodes are available: 1 node(s) were unschedulable")


def test_ready_timeout_defers_to_the_substrates_reason():
    """Campaign 38 live: four pods sat Pending on a cordoned node until the
    30-min timeout, and the explicit ready_timeout class buried the driver's
    'unschedulable' — turning blameless infrastructure into config-induced
    failures that taught the optimizer a healthy region crashes. The timeout
    class must yield to the substrate's concrete reason: the run fails as
    unschedulable (infrastructure) and the candidate gets its slot back."""
    supervisor, factory = make_supervisor()
    supervisor.driver = WedgedPodDriver()
    supervisor.tick()  # plan + schedule + launch

    with factory() as session:
        run = session.scalars(select(Run)).first()
        handle = supervisor.driver.attach(supervisor._spec_for(session, run))
        supervisor._capture_failure(
            session, run, handle,
            failure_class="ready_timeout", error="not ready after 30 min",
        )
        session.commit()

    with factory() as session:
        run = session.scalars(select(Run)).first()
        assert run.status == RunStatus.FAILED.value
        assert run.failure_class == "unschedulable"
        assert "unschedulable" in run.error
        # Infrastructure, not the config's fault: the candidate is queued again.
        assert run.candidate.status == CandidateStatus.VALID.value
