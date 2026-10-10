"""What the Resources page is told about a machine's lease.

A lease is the hand-over: the machine is given over free, campaigns run on it,
and ending the lease stops only what the platform launched. The page draws
three positions — leased, running campaigns, handed back — from the same
predicates the worker acts on.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.control.orchestrator import lifecycle
from app.db.base import Base
from app.db.models import (
    Campaign,
    CampaignStatus,
    Candidate,
    CandidateStatus,
    LeaseEndMode,
    LeaseState,
    Machine,
    MachineState,
    Run,
    RunStatus,
    User,
)


def _platform(
    *,
    machine_state=MachineState.AVAILABLE,
    lease_state=LeaseState.ACTIVE,
    campaign_status=CampaignStatus.ACTIVE,
    candidates=1,
):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add(User(id=1, username="u", password_hash="x"))
        session.add(
            Machine(id=1, name="node-24", host="10.0.0.1", gpu_count=8,
                    state=machine_state.value, lease_state=lease_state.value)
        )
        session.add(
            Campaign(id=21, owner_id=1, name="tonight", engine="sglang", image="img",
                     model_path="/m", served_model_name="m",
                     search_space={"grid": {"tp": [2]}}, status=campaign_status.value,
                     machine_names=["node-24"])
        )
        for i in range(candidates):
            session.add(
                Candidate(id=i + 1, campaign_id=21, config={"tp": 2},
                          config_hash=f"h{i}", status=CandidateStatus.VALID.value)
            )
        session.commit()
    return factory


def _live_run(factory) -> None:
    with factory() as session:
        session.add(Run(campaign_id=21, candidate_id=1, machine_id=1,
                        status=RunStatus.BENCHING.value, gpu_indices=[0, 1],
                        started_at=datetime.now(UTC)))
        session.commit()


def _describe(factory) -> lifecycle.MachineLifecycle:
    with factory() as session:
        return lifecycle.describe(session, session.get(Machine, 1), 150)


def test_a_machine_not_leased_is_at_the_start():
    view = _describe(_platform(machine_state=MachineState.AWAY, lease_state=LeaseState.NONE))
    assert (view.step, view.state) == (0, lifecycle.WAITING)
    assert "Lease it" in view.detail
    assert view.readiness == lifecycle.RETURNABLE


def test_a_leased_machine_nobody_wants_waits():
    view = _describe(_platform(campaign_status=CampaignStatus.PAUSED))
    assert (view.step, view.state) == (1, lifecycle.WAITING)
    assert not view.campaigns
    assert view.readiness == lifecycle.IDLE


def test_a_leased_machine_a_campaign_wants_is_ready_at_once():
    """Nothing to capture or clear first: the lease is the hand-over."""
    view = _describe(_platform())
    assert (view.step, view.state, view.headline) == (1, lifecycle.WORKING, "Ready")
    assert [c["id"] for c in view.campaigns] == [21]


def test_runs_in_flight_are_counted_with_their_cards():
    factory = _platform()
    _live_run(factory)
    view = _describe(factory)
    assert view.headline == "1 run(s) in flight"
    assert "2 of 8 cards" in view.detail
    assert view.readiness == lifecycle.BUSY
    assert "Nothing else on it is touched" in view.hand_back.summary


def test_a_polite_hand_back_finishes_what_is_running():
    factory = _platform(lease_state=LeaseState.DRAINING)
    with factory() as session:
        session.get(Machine, 1).lease_end_mode = LeaseEndMode.POLITE.value
        session.commit()
    _live_run(factory)
    view = _describe(factory)
    assert (view.step, view.headline) == (2, "Finishing up before hand-back")


def test_an_eager_hand_back_stops_runs():
    factory = _platform(lease_state=LeaseState.DRAINING)
    with factory() as session:
        session.get(Machine, 1).lease_end_mode = LeaseEndMode.EAGER.value
        session.commit()
    _live_run(factory)
    assert _describe(factory).headline == "Stopping runs to hand the machine back"


def test_a_hand_back_with_nothing_running_closes_on_its_own():
    view = _describe(_platform(lease_state=LeaseState.DRAINING))
    assert view.headline == "Handing the machine back"
    assert "straight back" in view.hand_back.summary


def test_a_released_machine_is_done():
    factory = _platform(machine_state=MachineState.AWAY, lease_state=LeaseState.RELEASED)
    view = _describe(factory)
    assert (view.step, view.state) == (2, lifecycle.DONE)


def test_a_paused_campaign_freezes_its_machine_lease():
    """Pause must mean frozen: while the only live claim on a machine is a
    paused campaign, its lease does not auto-expire and the platform touches
    nothing."""
    factory = _platform(campaign_status=CampaignStatus.PAUSED)
    with factory() as session:
        assert lifecycle.frozen_by_pause(session, session.get(Machine, 1))


def test_an_active_campaign_does_not_freeze_the_lease():
    factory = _platform(campaign_status=CampaignStatus.ACTIVE)
    with factory() as session:
        assert not lifecycle.frozen_by_pause(session, session.get(Machine, 1))


def test_the_worst_case_hand_back_follows_the_runs():
    factory = _platform()
    _live_run(factory)
    view = _describe(factory)
    assert view.returnable_at is not None
    assert view.returnable_at > datetime.now(UTC) - timedelta(minutes=1)


def test_the_view_serializes_what_the_page_reads():
    out = _describe(_platform()).as_dict()
    assert {"step", "state", "headline", "detail", "campaigns", "readiness",
            "returnable_at", "hand_back"} <= set(out)
    assert set(out["hand_back"]) == {"summary"}
