"""Managed campaigns: a plugin's work, run by the platform's own rules.

A plugin hands the platform a campaign spec; the platform checks it like any
campaign, records who owns it, and closes it only once nothing of it is
running. These are the guarantees a plugin builds on.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.control.managed import (
    ManagedCampaignRefused,
    Owner,
    create_managed_campaign,
    extend_managed_campaign,
    owners_of,
    pin_managed_dataset,
    record_event,
    start_managed_campaign,
    stop_managed_campaign,
    stop_reason,
)
from app.datasets import pinning
from app.datasets.profiles import DatasetBuild
from app.db.base import Base
from app.db.models import (
    Campaign,
    CampaignStatus,
    Candidate,
    Event,
    Policy,
    PolicySession,
    PolicySessionStatus,
    Run,
    RunStatus,
    User,
)

SPEC = {
    "name": "one config",
    "engine": "sglang",
    "image": "img",
    "model_path": "/models/m",
    "served_model_name": "m",
    "search_space": {"base": {"tp": 2}},
}
OWNER = Owner(plugin="example", label="Request #4", link="/requests/4")


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as s:
        s.add(User(id=1, username="u", password_hash="x"))
        s.commit()
        yield s


def test_a_spec_becomes_a_campaign_with_its_owner_on_record(session):
    campaign = create_managed_campaign(session, owner=OWNER, owner_id=1, spec=SPEC)
    session.commit()

    assert campaign.id and campaign.status == CampaignStatus.DRAFT.value
    assert campaign.search_space == {"base": {"tp": 2}}
    assert owners_of(session, [campaign.id]) == {campaign.id: OWNER}
    created = session.scalars(select(Event).where(Event.campaign_id == campaign.id)).one()
    assert (created.kind, created.actor) == ("campaign_created", "plugin:example")


def test_start_for_opens_a_window_now_the_way_force_start_does(session):
    moment = datetime(2026, 10, 14, 8, 0, tzinfo=UTC)
    campaign = create_managed_campaign(
        session, owner=OWNER, owner_id=1, spec=SPEC, start_for=timedelta(hours=3), at=moment
    )
    assert campaign.status == CampaignStatus.ACTIVE.value
    assert campaign.window_start == moment
    assert campaign.window_end == campaign.override_until == moment + timedelta(hours=3)


@pytest.mark.parametrize(
    ("change", "says"),
    [
        ({"engine": "trt"}, "engine must be sglang|vllm"),
        ({"search_space": {"range": {"c": {"min": 8, "max": 2, "step": 2}}}}, "below min"),
        ({"verify_top_k": 2}, "verify_top_k needs verify_benchmark_slug"),
        ({"policy_id": 99}, "no policy with id 99"),
        ({"benchmark_spec": {"kind": "random"}}, "names its benchmarks by slug"),
    ],
)
def test_a_spec_that_breaks_a_rule_is_refused_and_nothing_is_made(session, change, says):
    with pytest.raises(ManagedCampaignRefused, match=says):
        create_managed_campaign(session, owner=OWNER, owner_id=1, spec={**SPEC, **change})
    session.rollback()
    assert session.scalars(select(Campaign)).all() == []


def test_stopping_waits_until_nothing_is_running(session):
    campaign = create_managed_campaign(
        session, owner=OWNER, owner_id=1, spec=SPEC, start_for=timedelta(hours=3)
    )
    session.add(Candidate(id=1, campaign_id=campaign.id, config_hash="h", config={"tp": 2}))
    run = Run(id=1, campaign_id=campaign.id, candidate_id=1, status=RunStatus.BENCHING.value)
    session.add(run)
    session.flush()

    assert stop_managed_campaign(session, campaign, plugin="example") is False
    # The window is shut now, so the platform ends the live run as it ends
    # any campaign's at the end of a window; the campaign is not done yet.
    assert campaign.status == CampaignStatus.ACTIVE.value
    assert campaign.window_end <= datetime.now(UTC) and campaign.override_until is None

    run.status = RunStatus.KILLED.value
    assert stop_managed_campaign(session, campaign, plugin="example", reason="aborted") is True
    assert campaign.status == CampaignStatus.DONE.value
    assert stop_managed_campaign(session, campaign, plugin="example") is True  # idempotent
    done = session.scalars(select(Event).where(Event.kind == "campaign_done")).all()
    assert [(e.actor, e.payload) for e in done] == [("plugin:example", {"reason": "aborted"})]


def test_a_stopped_campaigns_runs_say_stopped_not_window_cutoff(session):
    """A Hub abort shuts the window, and the window clock ends the runs still
    going. Regression: they read "window cutoff", as if the clock had run out."""
    campaign = create_managed_campaign(
        session, owner=OWNER, owner_id=1, spec=SPEC, start_for=timedelta(hours=3)
    )
    assert stop_reason(session, campaign) is None
    session.add(Candidate(id=1, campaign_id=campaign.id, config_hash="h", config={"tp": 2}))
    session.add(Run(id=1, campaign_id=campaign.id, candidate_id=1,
                    status=RunStatus.BENCHING.value))
    session.flush()
    stop_managed_campaign(session, campaign, plugin="example", reason="aborted")
    session.flush()
    assert stop_reason(session, campaign) == "stopped (aborted) by example"

    # The window moved since (started again): the old stop no longer explains it.
    campaign.window_end = datetime.now(UTC) + timedelta(hours=1)
    assert stop_reason(session, campaign) is None


def test_a_plugin_writes_to_the_event_log_under_its_own_name(session):
    record_event(session, "request_started", plugin="example", payload={"request": 4})
    event = session.scalars(select(Event)).one()
    assert (event.actor, event.kind, event.payload) == (
        "plugin:example", "request_started", {"request": 4})


def test_an_event_a_person_asked_for_is_theirs(session):
    record_event(session, "request_cancelled", plugin="example", actor="alice")
    assert session.scalars(select(Event)).one().actor == "alice"


def test_a_plugin_starts_its_campaign_later_on_the_machines_it_chose(session):
    campaign = create_managed_campaign(session, owner=OWNER, owner_id=1, spec=SPEC)
    moment = datetime(2026, 10, 14, 8, 0, tzinfo=UTC)
    start_managed_campaign(
        session, campaign, plugin="example", start_for=timedelta(hours=2),
        machine_names=["gpu-a"], at=moment,
    )
    assert campaign.status == CampaignStatus.ACTIVE.value
    assert campaign.machine_names == ["gpu-a"]
    assert campaign.window_end == campaign.override_until == moment + timedelta(hours=2)
    with pytest.raises(ManagedCampaignRefused, match="already active"):
        start_managed_campaign(session, campaign, plugin="example", start_for=timedelta(hours=1))


def test_extending_moves_the_end_of_a_running_campaign_only(session):
    campaign = create_managed_campaign(session, owner=OWNER, owner_id=1, spec=SPEC)
    later = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    extend_managed_campaign(session, campaign, plugin="example", until=later)
    assert campaign.window_end is None  # not started: left alone
    start_managed_campaign(session, campaign, plugin="example", start_for=timedelta(hours=1))
    extend_managed_campaign(session, campaign, plugin="example", until=later)
    assert campaign.window_end == campaign.override_until == later


def test_a_pinned_dataset_is_held_before_the_campaign_starts(session):
    campaign = create_managed_campaign(
        session, owner=OWNER, owner_id=1, spec={**SPEC, "dataset_profile": "traffic"}
    )
    build = DatasetBuild(build_id="D7", sha256="abc", records=10)
    pin_managed_dataset(
        session, campaign, plugin="example", build=build, applied=pinning.APPLIED_CURRENT
    )
    session.flush()
    assert (campaign.dataset_build_id, campaign.dataset_sha256) == ("D7", "abc")
    assert pinning.holders(session, "traffic", "D7") == [campaign]

    pin_managed_dataset(session, campaign, plugin="example", build=None)
    assert campaign.dataset_build_id == "" and campaign.dataset_pinned_at is None


def test_stopping_aborts_a_live_policy_session(session):
    session.add(Policy(id=1, owner_id=1, name="p", image="img"))
    campaign = create_managed_campaign(
        session, owner=OWNER, owner_id=1, spec={**SPEC, "policy_id": 1},
        start_for=timedelta(hours=1),
    )
    live = PolicySession(
        campaign_id=campaign.id, policy_id=1, status=PolicySessionStatus.SEARCHING.value
    )
    session.add(live)
    session.flush()

    assert stop_managed_campaign(session, campaign, plugin="example") is False
    assert live.abort_requested_at is not None
    live.status = PolicySessionStatus.ABORTED.value
    assert stop_managed_campaign(session, campaign, plugin="example") is True


def test_a_plugin_changes_only_the_campaigns_it_runs(session):
    theirs = create_managed_campaign(session, owner=OWNER, owner_id=1, spec=SPEC)
    plain = Campaign(owner_id=1, name="by hand", engine="sglang", image="img",
                     model_path="/m", served_model_name="m", search_space={"base": {}})
    session.add(plain)
    session.flush()
    for campaign, plugin in ((theirs, "other"), (plain, "example")):
        with pytest.raises(ManagedCampaignRefused, match="is not run by plugin"):
            stop_managed_campaign(session, campaign, plugin=plugin)
        with pytest.raises(ManagedCampaignRefused, match="is not run by plugin"):
            start_managed_campaign(session, campaign, plugin=plugin, start_for=timedelta(1))
