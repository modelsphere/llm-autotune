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
            error="benchmark ran but did not pass: replay (uptime 0.06 < 0.99)",
            metrics={"replay.uptime": 0.06},
            raw={"passed": False, "runs": [{"module_name": "replay", "passed": False}]},
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


def test_a_campaign_activated_mid_tick_is_planned_not_finished(monkeypatch):
    """The API can make a campaign active after a tick's _plan has run and
    before its _schedule does. Regression: _schedule then saw no candidates and
    marked it DONE without ever running it (the demo's campaign, force-started
    the moment the worker came up)."""
    supervisor, factory = make_supervisor()
    real_plan = supervisor._plan
    monkeypatch.setattr(supervisor, "_plan", lambda session: None)  # not active yet at _plan

    supervisor.tick()
    with factory() as session:
        assert session.get(Campaign, 1).status == CampaignStatus.ACTIVE.value

    monkeypatch.setattr(supervisor, "_plan", real_plan)
    supervisor.tick()
    assert _run_status(factory) == RunStatus.LAUNCHING.value


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


def test_a_paused_campaign_freezes_its_expired_lease():
    """Seen live: an operator hit Pause meaning to end a lease.
    The lease kept its own clock, expired, and auto-drained. Paused means
    frozen: an overdue lease held only by a paused campaign is left untouched."""
    supervisor, factory = make_supervisor()
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


def _request_stop(factory):
    """What POST /runs/{id}/stop records."""
    with factory() as session:
        run = session.scalars(select(Run).order_by(Run.id.desc())).first()
        session.add(Event(actor="u", kind="stop_requested", run_id=run.id, payload={}))
        session.commit()


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
