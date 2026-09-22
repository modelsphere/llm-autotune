"""Confirmed teardown: a run's cards stay reserved until its container is
actually gone, and a machine the platform cannot clean is quarantined rather
than reused.

The failure this guards against: campaign A's run is killed at its window edge,
its container is slow to die, and campaign B is placed on the same cards on the
strength of a DB row that went terminal before the hardware was free.
"""

from datetime import timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.control.orchestrator.supervisor import Supervisor
from app.db.base import Base
from app.db.models import (
    BaselineStatus,
    Campaign,
    CampaignStatus,
    Candidate,
    CandidateStatus,
    Machine,
    MachineState,
    Run,
    RunKind,
    RunStatus,
    User,
)
from tests.test_supervisor import FakeDriver, PassEvaluator, make_supervisor


class LingeringDriver(FakeDriver):
    """SIGTERM is ignored — only a forced teardown removes the container, and if
    `unkillable`, not even that. Models a wedged engine that does not release
    its cards on its own."""

    def __init__(self, unkillable: bool = False):
        super().__init__(instant_ready=True)
        self.unkillable = unkillable

    def request_stop(self, handle) -> None:
        pass  # the graceful SIGTERM lands on deaf ears; container stays up

    def teardown(self, handle) -> None:
        self.torn_down.append(handle.container_name)
        if not self.unkillable:
            self._gone.add(handle.container_name)


def _tick_until_terminal(sup, factory, n: int = 12) -> str | None:
    terminal = {RunStatus.SUCCEEDED.value, RunStatus.FAILED.value, RunStatus.KILLED.value}
    for _ in range(n):
        sup.tick()
        with factory() as session:
            run = session.scalars(select(Run)).first()
            if run and run.status in terminal:
                return run.status
    return None


def _backdate_finished(factory, seconds: int) -> None:
    from app.control.orchestrator.lifecycle import now as _now

    with factory() as session:
        run = session.scalars(select(Run)).first()
        run.finished_at = _now() - timedelta(seconds=seconds)
        session.commit()


def test_a_lingering_container_holds_its_machine_until_the_janitor_confirms_it_gone():
    sup, factory = make_supervisor(instant_ready=True)
    sup.driver = LingeringDriver()

    assert _tick_until_terminal(sup, factory) == RunStatus.SUCCEEDED.value

    # SIGTERM was ignored, so the run is finished but its container is not
    # confirmed gone: the machine stays reserved, the cards stay held.
    with factory() as session:
        run = session.scalars(select(Run)).first()
        assert run.teardown_pending is True
        assert session.get(Machine, 1).state == MachineState.RESERVED.value
    assert sup.driver.torn_down == []  # only the (ignored) SIGTERM so far

    # Once the graceful grace elapses the janitor forces the removal...
    _backdate_finished(factory, sup.settings.teardown_grace_seconds + 5)
    sup.tick()
    assert sup.driver.torn_down == ["autotune-run-1"]

    # ...and the next pass sees it gone, clears the flag and frees the machine.
    sup.tick()
    with factory() as session:
        run = session.scalars(select(Run)).first()
        assert run.teardown_pending is False
        assert session.get(Machine, 1).state == MachineState.AVAILABLE.value


def test_a_container_that_never_dies_quarantines_the_machine():
    sup, factory = make_supervisor(instant_ready=True)
    sup.driver = LingeringDriver(unkillable=True)

    assert _tick_until_terminal(sup, factory) == RunStatus.SUCCEEDED.value

    # Past the give-up budget the janitor stops hammering and holds the box for
    # a human instead of handing it to the next run.
    _backdate_finished(factory, sup.settings.teardown_giveup_seconds + 5)
    sup.tick()

    with factory() as session:
        machine = session.get(Machine, 1)
        assert machine.needs_attention is True
        assert "would not tear down" in machine.attention_reason
        run = session.scalars(select(Run)).first()
        assert run.teardown_pending is False  # stopped retrying
        # A quarantined machine is no longer offered to scheduling.
        assert sup._usable_machines(session, session.get(Campaign, 1)) == []


def _occupancy_env():
    """A machine with one finished-but-not-yet-torn-down run on cards [0, 1],
    and a fresh candidate waiting — the exact A→B collision setup."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add(User(id=1, username="u", password_hash="x"))
        session.add(
            Machine(
                id=1, name="gpu-01", host="10.0.0.1", gpu_count=4,
                state=MachineState.RESERVED.value,
                baseline_status=BaselineStatus.CLEARED.value,
            )
        )
        session.add(
            Campaign(
                id=1, owner_id=1, name="c", engine="sglang", image="img",
                model_path="/m", served_model_name="m",
                search_space={"grid": {"tp_size": [2]}},
                status=CampaignStatus.ACTIVE.value,
            )
        )
        # The dying run: terminal, but its container is not confirmed gone, so
        # it still owns cards 0 and 1.
        session.add(
            Run(
                id=1, campaign_id=1, candidate_id=1, machine_id=1,
                kind=RunKind.EXPERIMENT.value, status=RunStatus.SUCCEEDED.value,
                gpu_indices=[0, 1], service_port=28200,
                container_name="autotune-run-1", teardown_pending=True,
            )
        )
        session.add(
            Candidate(
                id=1, campaign_id=1, config={"tp_size": 2}, config_hash="h1",
                status=CandidateStatus.EXHAUSTED.value,
            )
        )
        # The next config, ready to place.
        session.add(
            Candidate(
                id=2, campaign_id=1, config={"tp_size": 2}, config_hash="h2",
                status=CandidateStatus.VALID.value,
            )
        )
        session.commit()
    sup = Supervisor(
        session_factory=factory, driver_name="ssh_docker",
        health_evaluator=PassEvaluator(), bench_evaluator=PassEvaluator(),
    )
    sup.driver = FakeDriver(instant_ready=True)
    return sup, factory


def test_a_draining_run_keeps_its_cards_reserved_for_the_next_placement():
    sup, factory = _occupancy_env()
    with factory() as session:
        placed = sup._start_one(session, session.get(Campaign, 1))
        session.commit()
    assert placed is True
    with factory() as session:
        new_run = session.scalars(
            select(Run).where(Run.candidate_id == 2)
        ).first()
        # It must NOT reuse the dying run's cards; the packer skips [0, 1].
        assert new_run is not None
        assert new_run.gpu_indices == [2, 3]
        # And a distinct port, not the one the draining run still holds.
        assert new_run.service_port != 28200
