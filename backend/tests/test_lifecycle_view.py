"""What the Resources page is told, and that it agrees with what the worker does.

The page used to infer the stage from `state` and `baseline_status` alone.
Those two fields cannot say whether a canary is owed, so Capture and Clear read
as required steps — and pressing Clear tore down the service the canary exists
to measure. These tests pin the shared answer both callers now read.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
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
    machine_state=MachineState.AVAILABLE,
    baseline_status=BaselineStatus.NONE,
    baseline=BASELINE,
    campaign_status=CampaignStatus.ACTIVE,
    canary=True,
    window_end=None,
    candidates=1,
):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add(User(id=1, username="u", password_hash="x"))
        session.add(
            Machine(id=1, name="node-24", host="10.0.0.1", gpu_count=8,
                    state=machine_state.value, baseline=baseline,
                    baseline_status=baseline_status.value)
        )
        session.add(
            Campaign(id=21, owner_id=1, name="tonight", engine="sglang", image="img",
                     model_path="/m", served_model_name="m",
                     search_space={"grid": {"tp": [2]}}, status=campaign_status.value,
                     machine_names=["node-24"], run_baseline_canary=canary,
                     window_end=window_end)
        )
        for i in range(candidates):
            session.add(
                Candidate(id=i + 1, campaign_id=21, config={"tp": 2},
                          config_hash=f"h{i}", status=CandidateStatus.VALID.value)
            )
        session.commit()
    return factory


def _describe(factory) -> lifecycle.MachineLifecycle:
    with factory() as session:
        return lifecycle.describe(session, session.get(Machine, 1), 150)


# -- the stage a machine is actually at ---------------------------------------


def test_a_machine_still_held_by_production_is_at_the_start():
    view = _describe(_platform(machine_state=MachineState.AWAY))
    assert view.step == 0
    assert view.state == lifecycle.WAITING
    assert "Lease it" in view.detail


def test_a_borrowed_machine_nobody_wants_does_not_look_busy():
    """Production keeps running until some campaign actually needs the cards.

    Step 1 (Captured), not 0: the machine HAS been borrowed, and the step being
    waited for is the next one. A waiting step the machine has already passed
    made a borrowed machine read as still with production."""
    view = _describe(_platform(campaign_status=CampaignStatus.PAUSED))
    assert view.step == 1
    assert view.state == lifecycle.WAITING
    assert not view.campaigns


def test_a_borrowed_machine_a_campaign_wants_is_being_captured():
    view = _describe(_platform())
    assert view.step == 1
    assert view.state == lifecycle.WORKING
    assert [c["id"] for c in view.campaigns] == [21]
    assert "Nothing is stopped" in view.detail


def test_a_captured_machine_owes_a_canary_before_anything_is_cleared():
    view = _describe(_platform(baseline_status=BaselineStatus.CAPTURED))
    assert view.step == 2
    assert view.canary_pending, "Clear right now would destroy what the canary measures"


def test_a_captured_machine_with_a_passed_canary_is_about_to_be_cleared():
    factory = _platform(baseline_status=BaselineStatus.CAPTURED)
    with factory() as session:
        session.add(
            Run(campaign_id=21, candidate_id=1, machine_id=1,
                kind=RunKind.BASELINE.value, status=RunStatus.SUCCEEDED.value)
        )
        session.commit()
    view = _describe(factory)
    assert view.step == 3
    assert not view.canary_pending


def test_a_failed_canary_is_reported_as_needing_a_human():
    factory = _platform(baseline_status=BaselineStatus.CAPTURED)
    with factory() as session:
        for _ in range(lifecycle.MAX_CANARY_ATTEMPTS_PER_START):
            session.add(
                Run(campaign_id=21, candidate_id=1, machine_id=1,
                    kind=RunKind.BASELINE.value, status=RunStatus.FAILED.value)
            )
        session.commit()
    view = _describe(factory)
    assert view.state == lifecycle.BLOCKED
    assert "left running" in view.detail


def test_a_cleared_machine_with_work_queued_is_ready_for_experiments():
    view = _describe(_platform(baseline_status=BaselineStatus.CLEARED))
    assert view.step == 3
    assert view.state == lifecycle.WORKING


def test_a_cleared_machine_with_no_window_says_nobody_will_restore_it():
    """Campaign 21 had no window, so production would have stayed down until
    someone noticed. Silence was the worst answer available."""
    view = _describe(
        _platform(baseline_status=BaselineStatus.CLEARED,
                  campaign_status=CampaignStatus.DONE)
    )
    assert view.state == lifecycle.WAITING
    assert "nothing will put it back" in view.detail


def test_a_closed_window_shows_production_coming_back():
    view = _describe(
        _platform(baseline_status=BaselineStatus.CLEARED,
                  campaign_status=CampaignStatus.DONE,
                  window_end=datetime.now(UTC) - timedelta(minutes=5))
    )
    assert view.step == 4
    assert "Restoring" in view.headline


def test_a_window_that_expired_before_the_clear_promises_nothing():
    """node-24, live, 2026-08-04: the page said "Restoring production" on the
    orders of a campaign whose window closed four days before the machine was
    cleared — a restore the worker was never going to perform. A page that
    promises what the worker will not do is worse than one that says nothing."""
    factory = _platform(baseline_status=BaselineStatus.CLEARED,
                        campaign_status=CampaignStatus.DONE,
                        window_end=datetime.now(UTC) - timedelta(days=4))
    with factory() as session:
        session.add(Event(actor="admin", kind="baseline_cleared",
                          payload={"machine": "node-24"},
                          ts=datetime.now(UTC) - timedelta(minutes=10)))
        session.commit()

    view = _describe(factory)
    assert view.state == lifecycle.WAITING
    assert "nothing will put it back" in view.detail
    with factory() as session:
        assert not lifecycle.restore_due(session, session.get(Machine, 1))


def test_a_paused_campaign_freezes_its_machine_lease():
    """Seen live: an operator hit Pause meaning to end a lease,
    expecting a paused campaign to leave the machine alone. It did not — the
    lease kept its own clock, expired, and auto-drained, restarting production
    over a service the operator had restored by hand. Pause must mean frozen:
    while the only live claim on a machine is a paused campaign, its lease does
    not auto-expire and the platform touches nothing."""
    factory = _platform(campaign_status=CampaignStatus.PAUSED)
    with factory() as session:
        assert lifecycle.frozen_by_pause(session, session.get(Machine, 1))


def test_an_active_campaign_does_not_freeze_the_lease():
    """The freeze is only for a pause with no other live claim: an ACTIVE
    campaign on the machine keeps the normal lease-expiry safety net."""
    factory = _platform(campaign_status=CampaignStatus.ACTIVE)
    with factory() as session:
        assert not lifecycle.frozen_by_pause(session, session.get(Machine, 1))


def test_capture_with_nothing_running_needs_no_canary():
    """An empty machine has no production to measure — the page must not sit
    on a step that will never advance."""
    factory = _platform(baseline_status=BaselineStatus.CAPTURED, baseline={"services": []})
    view = _describe(factory)
    assert not view.canary_pending


def test_the_page_is_told_when_automation_is_off():
    factory = _platform()
    with factory() as session:
        view = lifecycle.describe(session, session.get(Machine, 1), 150, auto=False)
    assert view.state == lifecycle.BLOCKED
    assert "by hand" in view.detail


def test_automation_being_off_does_not_erase_where_the_machine_actually_is():
    """Seen live: both read "Leased / Automatic
    hand-over is off" at step 0 while actually sitting past Cleared with an
    empty capture. Switching automation off says nothing will MOVE the machine;
    it does not make its position unknown, and collapsing every machine to one
    banner is how two very different boxes came to look identical."""
    factory = _platform(baseline_status=BaselineStatus.CLEARED,
                        baseline={"services": []},
                        campaign_status=CampaignStatus.DONE)
    with factory() as session:
        machine = session.get(Machine, 1)
        auto = lifecycle.describe(session, machine, 150, auto=True)
        off = lifecycle.describe(session, machine, 150, auto=False)

    assert off.step == auto.step, "the step reached does not depend on the switch"
    assert off.headline == auto.headline
    assert "nothing was running" in off.headline.lower()


def test_a_working_step_is_not_promised_while_automation_is_off():
    """The inverse: with the switch off, "Recording production" is not
    something happening, it is something that will not happen until a human
    presses Capture. The headline stays; the promise does not."""
    factory = _platform()
    with factory() as session:
        machine = session.get(Machine, 1)
        assert lifecycle.describe(session, machine, 150, auto=True).state == lifecycle.WORKING
        off = lifecycle.describe(session, machine, 150, auto=False)
    assert off.state == lifecycle.BLOCKED
    assert off.step == 1, "still the step being waited for"
    assert "Override menu" in off.detail


def test_a_drain_keeps_moving_even_with_automation_off():
    """`_advance_leases` is not gated on the baseline-lifecycle switch, so a
    draining machine really is being handed back on its own. Marking that
    blocked would send an operator looking for a button to press."""
    factory = _platform(baseline_status=BaselineStatus.CLEARED)
    with factory() as session:
        machine = session.get(Machine, 1)
        machine.lease_state = LeaseState.DRAINING.value
        view = lifecycle.describe(session, machine, 150, auto=False)
    assert view.state == lifecycle.WORKING
    assert "Override menu" not in view.detail


def test_an_empty_capture_does_not_claim_production_is_down():
    """`cleared` covers both "we stopped production" and "there was none to
    stop". Only the first is owed a restore, and saying "production is down"
    about the second is how an operator comes to expect one on hand-back."""
    factory = _platform(baseline_status=BaselineStatus.CLEARED,
                        baseline={"services": []},
                        campaign_status=CampaignStatus.DONE)
    view = _describe(factory)
    assert "Production is down" not in view.detail
    assert "nothing to stop" in view.detail or "nothing was stopped" in view.detail
    assert not view.hand_back.restores and not view.hand_back.owed
    # Not a hollow "Restored" step: that is the page promising a restore that
    # is never coming.
    assert view.step == lifecycle.STEPS.index("Cleared")
    assert view.state == lifecycle.DONE


def test_a_cleared_machine_we_emptied_still_says_production_is_down():
    factory = _platform(baseline_status=BaselineStatus.CLEARED,
                        campaign_status=CampaignStatus.DONE)
    view = _describe(factory)
    assert "Production is down" in view.detail
    assert view.hand_back.services == 1


def test_the_view_carries_what_ending_the_lease_will_do():
    """The card and the confirm dialog read one string, computed from the
    branch the drain takes — not two sentences written in two files."""
    factory = _platform(baseline_status=BaselineStatus.CLEARED,
                        campaign_status=CampaignStatus.DONE)
    with factory() as session:
        machine = session.get(Machine, 1)
        owed = lifecycle.describe(session, machine, 150, auto_restore=False)
        restores = lifecycle.describe(session, machine, 150, auto_restore=True)
    assert owed.hand_back.owed and "stays DOWN" in owed.hand_back.summary
    assert restores.hand_back.restores
    assert owed.as_dict()["hand_back"]["summary"] == owed.hand_back.summary


# -- the page and the worker must not disagree --------------------------------


def test_canary_due_is_the_same_question_the_supervisor_asks():
    """The scheduler calls _baseline_canary_due; the page calls canary_state.
    A second implementation of the same predicate is how the two views of a
    machine came apart in the first place."""
    factory = _platform(baseline_status=BaselineStatus.CAPTURED)
    supervisor = Supervisor(
        session_factory=factory,
        health_evaluator=StubEvaluator(),
        bench_evaluator=StubEvaluator(),
    )
    supervisor.driver = CompletingDriver()

    with factory() as session:
        machine = session.get(Machine, 1)
        campaign = session.get(Campaign, 21)
        assert supervisor._baseline_canary_due(session, campaign, machine) is True
        assert lifecycle.canary_state(session, campaign, machine) == lifecycle.CANARY_DUE

        campaign.run_baseline_canary = False
        assert supervisor._baseline_canary_due(session, campaign, machine) is False
        assert lifecycle.canary_state(session, campaign, machine) == (
            lifecycle.CANARY_NOT_REQUIRED
        )
