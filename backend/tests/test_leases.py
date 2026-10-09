"""Handing a machine back.

A lease is the hand-over: the machine comes to the platform free and goes back
when the lease ends, with only the platform's own runs stopped. The modes
differ only in what happens to those runs on the way.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.control.orchestrator import lifecycle
from app.control.orchestrator.supervisor import Supervisor
from app.db.base import Base
from app.db.models import (
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


def _platform(
    *,
    lease_state=LeaseState.ACTIVE,
    end_mode="",
    deadline_at=None,
    due_at=None,
    live_run=False,
):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add(User(id=1, username="u", password_hash="x"))
        session.add(
            Machine(id=1, name="node-24", host="10.0.0.1", gpu_count=8,
                    state=MachineState.AVAILABLE.value,
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


def test_a_polite_end_with_nothing_running_releases_at_once():
    supervisor, factory = _platform(lease_state=LeaseState.DRAINING,
                                    end_mode=LeaseEndMode.POLITE.value)

    supervisor.tick()

    machine = _machine(factory)
    assert machine.lease_state == LeaseState.RELEASED.value
    assert machine.state == MachineState.AWAY.value


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


# -- the machine goes back once our runs are down -----------------------------


def test_an_eager_end_releases_once_its_runs_are_killed():
    supervisor, factory = _platform(lease_state=LeaseState.DRAINING,
                                    end_mode=LeaseEndMode.EAGER.value, live_run=True)
    for _ in range(4):
        supervisor.tick()
        if _machine(factory).lease_state == LeaseState.RELEASED.value:
            break
    else:
        raise AssertionError("the drain never completed")
    assert "lease_released" in _kinds(factory)


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
    _, factory = _platform(lease_state=LeaseState.RELEASED)
    with factory() as session:
        assert lifecycle.readiness(session, session.get(Machine, 1)) == lifecycle.RETURNABLE


def test_an_idle_leased_machine_is_idle_not_returnable():
    """Ours and free: the holder can end the lease, but it is not theirs yet."""
    _, factory = _platform(lease_state=LeaseState.ACTIVE)
    with factory() as session:
        assert lifecycle.readiness(session, session.get(Machine, 1)) == lifecycle.IDLE


def test_hand_back_says_only_our_runs_stop():
    _, factory = _platform(live_run=True)
    with factory() as session:
        summary = lifecycle.hand_back(session, session.get(Machine, 1)).summary
    assert "Nothing else on it is touched" in summary
