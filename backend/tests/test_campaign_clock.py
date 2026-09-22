"""A campaign running itself on a clock.

The status transitions are the whole feature: nobody is awake at 23:00, and a
nightly job that needs a human to press Start is not a nightly job.
"""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

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
    Event,
    LeaseState,
    Machine,
    MachineState,
    User,
)
from tests.fakes import NullDriver, StubEvaluator

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _now_at(hour: int, minute: int = 0) -> datetime:
    """A UTC instant that is `hour:minute` local time today in Shanghai."""
    today = datetime.now(SHANGHAI).date()
    return datetime.combine(today, time(hour, minute), tzinfo=SHANGHAI).astimezone(UTC)


def _platform(*, daily_start="23:00", daily_end="08:00", status=CampaignStatus.SCHEDULED,
              schedule_until=None):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add(User(id=1, username="u", password_hash="x"))
        session.add(
            Machine(id=1, name="node-24", host="10.0.0.1", gpu_count=8,
                    state=MachineState.AVAILABLE.value, baseline={"services": []},
                    baseline_status=BaselineStatus.CLEARED.value,
                    lease_state=LeaseState.ACTIVE.value,
                    lease_due_at=datetime.now(UTC) + timedelta(days=1))
        )
        session.add(
            Campaign(id=21, owner_id=1, name="nightly", engine="sglang", image="img",
                     model_path="/m", served_model_name="m",
                     search_space={"grid": {"tp": [2]}}, status=status.value,
                     machine_names=["node-24"], run_baseline_canary=False,
                     max_run_minutes=60, daily_start=daily_start, daily_end=daily_end,
                     schedule_timezone="Asia/Shanghai", schedule_until=schedule_until)
        )
        session.add(
            Candidate(id=1, campaign_id=21, config={"tp": 2}, config_hash="h1",
                      status=CandidateStatus.VALID.value)
        )
        session.commit()
    supervisor = Supervisor(
        session_factory=factory,
        health_evaluator=StubEvaluator(),
        bench_evaluator=StubEvaluator(),
    )
    supervisor.driver = NullDriver()
    return supervisor, factory


def _status(factory) -> str:
    with factory() as session:
        return session.get(Campaign, 21).status


def _kinds(factory) -> list[str]:
    with factory() as session:
        return [e.kind for e in session.scalars(select(Event)).all()]


def _at(monkeypatch, moment: datetime) -> None:
    """Freeze the clock the scheduler reads."""
    import app.control.orchestrator.supervisor as sup

    monkeypatch.setattr(sup, "_now", lambda: moment)
    monkeypatch.setattr("app.control.orchestrator.schedule.now_utc", lambda: moment)


# -- waking and standing down -------------------------------------------------


def test_a_scheduled_campaign_wakes_when_its_window_opens(monkeypatch):
    supervisor, factory = _platform()
    _at(monkeypatch, _now_at(23, 5))

    supervisor.tick()

    assert _status(factory) == CampaignStatus.ACTIVE.value
    assert "campaign_window_opened" in _kinds(factory)
    with factory() as session:
        campaign = session.get(Campaign, 21)
        assert campaign.window_start is not None and campaign.window_end is not None


def test_a_scheduled_campaign_stays_asleep_outside_its_window(monkeypatch):
    supervisor, factory = _platform()
    _at(monkeypatch, _now_at(12, 0))

    supervisor.tick()

    assert _status(factory) == CampaignStatus.SCHEDULED.value
    assert "campaign_window_opened" not in _kinds(factory)


def test_an_active_campaign_stands_down_at_the_end_of_its_window(monkeypatch):
    supervisor, factory = _platform(status=CampaignStatus.ACTIVE)
    _at(monkeypatch, _now_at(9, 0))

    supervisor.tick()

    assert _status(factory) == CampaignStatus.SCHEDULED.value
    assert "campaign_window_closed" in _kinds(factory)


def test_standing_down_and_waking_again_is_the_same_campaign(monkeypatch):
    """The point of recurring: tomorrow night resumes this search rather than
    needing a new campaign built by hand."""
    supervisor, factory = _platform()

    _at(monkeypatch, _now_at(23, 30))
    supervisor.tick()
    assert _status(factory) == CampaignStatus.ACTIVE.value

    _at(monkeypatch, _now_at(9, 0) + timedelta(days=1))
    supervisor.tick()
    assert _status(factory) == CampaignStatus.SCHEDULED.value

    _at(monkeypatch, _now_at(23, 30) + timedelta(days=1))
    supervisor.tick()
    assert _status(factory) == CampaignStatus.ACTIVE.value

    with factory() as session:
        assert session.get(Candidate, 1) is not None, "the search carried over"


# -- what the clock must not override -----------------------------------------


def test_a_paused_campaign_is_not_woken_by_its_schedule(monkeypatch):
    """A human holding a campaign outranks the clock. Otherwise pausing at
    23:30 would be undone on the next tick and there would be no way to stop a
    nightly job short of deleting it."""
    supervisor, factory = _platform(status=CampaignStatus.PAUSED)
    _at(monkeypatch, _now_at(23, 30))

    supervisor.tick()

    assert _status(factory) == CampaignStatus.PAUSED.value


def test_a_campaign_with_no_schedule_is_left_alone(monkeypatch):
    supervisor, factory = _platform(daily_start="", daily_end="",
                                    status=CampaignStatus.ACTIVE)
    _at(monkeypatch, _now_at(12, 0))

    supervisor.tick()

    assert _status(factory) == CampaignStatus.ACTIVE.value


# -- the end of the arrangement -----------------------------------------------


def test_a_campaign_past_its_until_date_finishes_rather_than_rescheduling(monkeypatch):
    supervisor, factory = _platform(
        status=CampaignStatus.ACTIVE, schedule_until=_now_at(8, 0) - timedelta(days=1)
    )
    _at(monkeypatch, _now_at(9, 0))

    supervisor.tick()

    assert _status(factory) == CampaignStatus.DONE.value
    assert "campaign_schedule_finished" in _kinds(factory)


# -- force start: a human outranking the clock, for a while -------------------


def test_an_override_keeps_a_campaign_awake_outside_its_window(monkeypatch):
    """Why force start needs an override at all: marking a scheduled campaign
    active is otherwise undone on the very next tick, because the schedule sees
    no open window and puts it straight back to sleep."""
    supervisor, factory = _platform(status=CampaignStatus.ACTIVE)
    noon = _now_at(12, 0)
    with factory() as session:
        campaign = session.get(Campaign, 21)
        campaign.override_until = noon + timedelta(hours=8)
        campaign.window_start, campaign.window_end = noon, noon + timedelta(hours=8)
        session.commit()
    _at(monkeypatch, noon)

    supervisor.tick()

    assert _status(factory) == CampaignStatus.ACTIVE.value


def test_without_an_override_an_active_campaign_outside_its_window_stands_down(monkeypatch):
    supervisor, factory = _platform(status=CampaignStatus.ACTIVE)
    _at(monkeypatch, _now_at(12, 0))

    supervisor.tick()

    assert _status(factory) == CampaignStatus.SCHEDULED.value


def test_an_expired_override_hands_control_back_to_the_clock(monkeypatch):
    supervisor, factory = _platform(status=CampaignStatus.ACTIVE)
    noon = _now_at(12, 0)
    with factory() as session:
        session.get(Campaign, 21).override_until = noon - timedelta(minutes=1)
        session.commit()
    _at(monkeypatch, noon)

    supervisor.tick()

    assert _status(factory) == CampaignStatus.SCHEDULED.value
    assert "campaign_override_expired" in _kinds(factory)
    with factory() as session:
        assert session.get(Campaign, 21).override_until is None


def test_an_override_does_not_resurrect_a_paused_campaign(monkeypatch):
    """Force stop clears the override precisely so this cannot happen; the
    scheduler only looks at scheduled and active campaigns, and this pins that
    it stays that way."""
    supervisor, factory = _platform(status=CampaignStatus.PAUSED)
    noon = _now_at(12, 0)
    with factory() as session:
        session.get(Campaign, 21).override_until = noon + timedelta(hours=8)
        session.commit()
    _at(monkeypatch, noon)

    supervisor.tick()

    assert _status(factory) == CampaignStatus.PAUSED.value


def test_the_window_is_rewritten_each_tick_so_an_edit_takes_effect_tonight(monkeypatch):
    """Editing a running campaign's schedule has to apply now, not next week."""
    supervisor, factory = _platform(status=CampaignStatus.ACTIVE)
    _at(monkeypatch, _now_at(23, 30))
    supervisor.tick()
    with factory() as session:
        first = session.get(Campaign, 21).window_end

    with factory() as session:
        session.get(Campaign, 21).daily_end = "06:00"
        session.commit()
    supervisor.tick()

    with factory() as session:
        assert session.get(Campaign, 21).window_end != first
