"""Handing a machine back.

The promise the lease API makes is narrow and absolute: when `readiness` reads
`returnable`, the production service we found on the machine is running again.
Every test here is ultimately about that — the modes differ only in what
happens to our own benchmarks on the way.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.control.orchestrator import lifecycle
from app.control.orchestrator.supervisor import Supervisor
from app.db.base import Base
from app.db.models import (
    BaselineStatus,
    Campaign,
    CampaignStatus,
    Candidate,
    CandidateStatus,
    Event,
    LeaseEndMode,
    LeaseState,
    Machine,
    MachineState,
    Run,
    RunKind,
    RunStatus,
    User,
)
from tests.fakes import CompletingDriver, StubEvaluator

BASELINE = {
    "services": [
        {
            "container": "sglang-prod-p8050",
            "endpoint_url": "http://10.0.0.1:8050",
            "served_model_name": "glm-5",
            "port": "8050",
        }
    ]
}


def _platform(
    *,
    lease_state=LeaseState.ACTIVE,
    end_mode="",
    deadline_at=None,
    due_at=None,
    baseline_status=BaselineStatus.CLEARED,
    live_run=False,
    auto_restore=True,
):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add(User(id=1, username="u", password_hash="x"))
        session.add(
            Machine(id=1, name="node-24", host="10.0.0.1", gpu_count=8,
                    state=MachineState.AVAILABLE.value, baseline=BASELINE,
                    baseline_status=baseline_status.value,
                    lease_state=lease_state.value, lease_holder="key:fleet",
                    lease_end_mode=end_mode, lease_deadline_at=deadline_at,
                    lease_due_at=due_at, leased_at=datetime.now(UTC))
        )
        session.add(
            Campaign(id=21, owner_id=1, name="tonight", engine="sglang", image="img",
                     model_path="/m", served_model_name="m",
                     search_space={"grid": {"tp": [2]}},
                     status=CampaignStatus.ACTIVE.value, machine_names=["node-24"],
                     max_run_minutes=150, run_baseline_canary=False)
        )
        session.add(
            Candidate(id=1, campaign_id=21, config={"tp": 2}, config_hash="h1",
                      status=CandidateStatus.VALID.value)
        )
        if live_run:
            session.add(
                Run(id=1, campaign_id=21, candidate_id=1, machine_id=1,
                    kind=RunKind.EXPERIMENT.value, status=RunStatus.BENCHING.value,
                    gpu_indices=[0, 1], started_at=datetime.now(UTC))
            )
        session.commit()

    supervisor = Supervisor(
        session_factory=factory,
        health_evaluator=StubEvaluator({"probe_output_chars": 2}),
        bench_evaluator=StubEvaluator({"score_total": 1.0}),
    )
    supervisor.driver = CompletingDriver()
    # The put-back at lease end is opt-in; these lease tests exercise it, so
    # default it on and let a case pass False for the admin-owned default.
    supervisor.settings = supervisor.settings.model_copy(
        update={"auto_restore_production": auto_restore}
    )
    return supervisor, factory


def _machine(factory) -> Machine:
    with factory() as session:
        return session.get(Machine, 1)


def _kinds(factory) -> list[str]:
    with factory() as session:
        return [e.kind for e in session.scalars(select(Event)).all()]


# -- a polite hand-back waits -------------------------------------------------


def test_a_polite_end_lets_a_running_benchmark_finish():
    supervisor, factory = _platform(lease_state=LeaseState.DRAINING,
                                    end_mode=LeaseEndMode.POLITE.value, live_run=True)

    supervisor.tick()

    with factory() as session:
        run = session.get(Run, 1)
        # Not "still benching": the stub evaluator finishes it inside the same
        # tick, which is exactly what a polite end wants. The claim is that the
        # lease did not cut it short.
        assert run.status != RunStatus.KILLED.value, "a polite end does not kill work"
        assert run.error != "lease ended"


def test_a_draining_machine_takes_no_new_work():
    """The promise made to the lease holder is that nothing new starts. A
    placement after that promise is one that has to be killed to keep it."""
    machine = Machine(name="node-24", state=MachineState.AVAILABLE.value,
                      lease_state=LeaseState.DRAINING.value)
    assert not lifecycle.accepts_new_work(machine)
    machine.lease_state = LeaseState.ACTIVE.value
    assert lifecycle.accepts_new_work(machine)


def test_a_polite_end_with_nothing_running_restores_production_and_releases():
    supervisor, factory = _platform(lease_state=LeaseState.DRAINING,
                                    end_mode=LeaseEndMode.POLITE.value)

    supervisor.tick()  # restores production
    supervisor.tick()  # closes the lease

    machine = _machine(factory)
    assert machine.baseline_status == BaselineStatus.RESTORED.value
    assert machine.lease_state == LeaseState.RELEASED.value
    assert machine.state == MachineState.AWAY.value
    assert supervisor.driver.restored == ["node-24"]


# -- an eager hand-back does not ----------------------------------------------


def test_an_eager_end_kills_the_running_benchmark():
    supervisor, factory = _platform(lease_state=LeaseState.DRAINING,
                                    end_mode=LeaseEndMode.EAGER.value, live_run=True)

    supervisor.tick()

    with factory() as session:
        run = session.get(Run, 1)
        assert run.status == RunStatus.KILLED.value
        assert run.error == "lease ended"


def test_a_polite_end_starts_killing_once_its_deadline_arrives():
    """A promise to be off by a time is still a promise when keeping it costs a
    benchmark."""
    supervisor, factory = _platform(
        lease_state=LeaseState.DRAINING, end_mode=LeaseEndMode.POLITE.value,
        deadline_at=datetime.now(UTC) - timedelta(seconds=1), live_run=True,
    )

    supervisor.tick()

    with factory() as session:
        assert session.get(Run, 1).status == RunStatus.KILLED.value


# -- production always comes back ---------------------------------------------


def test_the_machine_is_never_released_with_production_still_down():
    """The one guarantee the lease API makes. An eager end kills the
    benchmarks, not the restore."""
    supervisor, factory = _platform(lease_state=LeaseState.DRAINING,
                                    end_mode=LeaseEndMode.EAGER.value, live_run=True)

    for _ in range(4):
        supervisor.tick()
        machine = _machine(factory)
        if machine.lease_state == LeaseState.RELEASED.value:
            assert machine.baseline_status == BaselineStatus.RESTORED.value
            break
    else:
        raise AssertionError("the drain never completed")


def test_auto_restore_off_releases_the_machine_with_production_left_down():
    """The deliberate inverse, and the default: with auto-restore off the lease
    end does NOT relaunch production. The machine is handed back with production
    as we left it and a `production_left_down` event flags the manual restore
    the admin owns. The never-release-with-prod-down guarantee holds only when
    auto-restore is enabled."""
    supervisor, factory = _platform(lease_state=LeaseState.DRAINING,
                                    end_mode=LeaseEndMode.EAGER.value, live_run=True,
                                    auto_restore=False)
    for _ in range(4):
        supervisor.tick()
        machine = _machine(factory)
        if machine.lease_state == LeaseState.RELEASED.value:
            break
    else:
        raise AssertionError("the drain never completed")

    assert supervisor.driver.restored == [], "auto-restore off: must not relaunch production"
    assert machine.baseline_status != BaselineStatus.RESTORED.value
    assert "production_left_down" in _kinds(factory)


def test_a_machine_with_nothing_captured_releases_without_a_restore():
    """Capture found no production services, so there is nothing to put back —
    and waiting for a restore that can never happen would strand the lease."""
    supervisor, factory = _platform(lease_state=LeaseState.DRAINING,
                                    end_mode=LeaseEndMode.POLITE.value)
    with factory() as session:
        session.get(Machine, 1).baseline = {"services": []}
        session.commit()

    supervisor.tick()

    assert _machine(factory).lease_state == LeaseState.RELEASED.value
    assert supervisor.driver.restored == [], "nothing was captured, nothing to restore"


# -- a lease that lapses ------------------------------------------------------


def test_an_overdue_lease_drains_itself():
    """The holder planned around getting the machine back. A lease that lapses
    quietly is worse than one that ends loudly."""
    supervisor, factory = _platform(
        lease_state=LeaseState.ACTIVE,
        due_at=datetime.now(UTC) - timedelta(minutes=1),
        live_run=True,
    )

    supervisor.tick()

    machine = _machine(factory)
    assert machine.lease_state == LeaseState.DRAINING.value
    assert machine.lease_end_mode == LeaseEndMode.POLITE.value
    assert "lease_end_requested" in _kinds(factory)
    with factory() as session:
        assert session.get(Run, 1).status != RunStatus.KILLED.value, "overdue is not eager"


def test_a_lease_inside_its_term_is_left_alone():
    supervisor, factory = _platform(
        lease_state=LeaseState.ACTIVE, due_at=datetime.now(UTC) + timedelta(hours=8)
    )

    supervisor.tick()

    assert _machine(factory).lease_state == LeaseState.ACTIVE.value


# -- what the external caller is told -----------------------------------------


def test_readiness_reports_busy_while_our_runs_hold_the_machine():
    _, factory = _platform(live_run=True)
    with factory() as session:
        machine = session.get(Machine, 1)
        assert lifecycle.readiness(session, machine) == lifecycle.BUSY
        free_at = lifecycle.returnable_at(session, machine)
    assert free_at is not None, "a busy machine must say when it will be free"


def test_returnable_at_is_the_worst_case_not_an_average():
    """A caller planning around this needs a bound it can rely on, so it is the
    run's own cutoff — started_at + max_run_minutes — not a typical duration."""
    _, factory = _platform(live_run=True)
    with factory() as session:
        run = session.get(Run, 1)
        started = run.started_at
        free_at = lifecycle.returnable_at(session, session.get(Machine, 1))
    assert free_at == lifecycle.as_utc(started) + timedelta(minutes=150)


def test_readiness_reports_returnable_once_the_lease_is_closed():
    _, factory = _platform(lease_state=LeaseState.RELEASED,
                           baseline_status=BaselineStatus.RESTORED)
    with factory() as session:
        assert lifecycle.readiness(session, session.get(Machine, 1)) == lifecycle.RETURNABLE


def test_an_idle_machine_with_production_down_is_not_returnable():
    """Idle is not the same as ready to hand over: the GPUs are free but the
    service we displaced is still stopped."""
    _, factory = _platform(lease_state=LeaseState.ACTIVE,
                           baseline_status=BaselineStatus.CLEARED)
    with factory() as session:
        assert lifecycle.readiness(session, session.get(Machine, 1)) == lifecycle.IDLE


# -- the dialog and the drain must not disagree -------------------------------


def _predicted(factory, auto_restore: bool) -> lifecycle.HandBack:
    with factory() as session:
        return lifecycle.hand_back(session.get(Machine, 1), auto_restore)


def test_hand_back_predicts_the_restore_the_drain_performs():
    """`hand_back` is what the End lease dialog tells the operator. If it can
    say "restored" while the drain leaves production down, the dialog is worse
    than silence — which is exactly what the unconditional "production is
    restored either way" text was."""
    supervisor, factory = _platform(lease_state=LeaseState.DRAINING,
                                    end_mode=LeaseEndMode.POLITE.value)
    predicted = _predicted(factory, auto_restore=True)
    assert predicted.restores and not predicted.owed
    assert predicted.services == 1

    # The restore is confirmed before the lease closes, so this takes more
    # than one tick — which is itself the "minutes while the model loads" the
    # dialog warns about.
    for _ in range(4):
        supervisor.tick()
        if _machine(factory).lease_state == LeaseState.RELEASED.value:
            break
    else:
        raise AssertionError("the drain never completed")
    assert supervisor.driver.restored, "predicted a restore; the drain did not do one"


def test_hand_back_predicts_production_being_left_down():
    """The default deployment. The operator has to learn this BEFORE pressing
    the button, not from a `production_left_down` event afterwards."""
    supervisor, factory = _platform(lease_state=LeaseState.DRAINING,
                                    end_mode=LeaseEndMode.POLITE.value,
                                    auto_restore=False)
    predicted = _predicted(factory, auto_restore=False)
    assert predicted.owed and not predicted.restores
    assert "stays DOWN" in predicted.summary

    supervisor.tick()

    assert supervisor.driver.restored == [], "predicted no restore; the drain did one"
    assert "production_left_down" in _kinds(factory)


def test_hand_back_predicts_nothing_to_do_for_an_empty_capture():
    """Seen live: leased, cleared, and captured
    nothing, because production was already down when they were handed over.
    Ending those leases touches nothing at all — and the page said the same
    thing about them as about a machine whose production we had torn down."""
    _, factory = _platform(lease_state=LeaseState.ACTIVE)
    with factory() as session:
        session.get(Machine, 1).baseline = {"services": []}
        session.commit()

    for auto_restore in (True, False):
        predicted = _predicted(factory, auto_restore)
        assert not predicted.restores and not predicted.owed
        assert predicted.services == 0
        assert "nothing to put back" in predicted.summary


def test_hand_back_says_nothing_is_touched_while_production_is_still_up():
    _, factory = _platform(lease_state=LeaseState.ACTIVE,
                           baseline_status=BaselineStatus.CAPTURED)
    predicted = _predicted(factory, auto_restore=True)
    assert not predicted.restores and not predicted.owed
    assert "never stopped" in predicted.summary
