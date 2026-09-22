"""One campaign, one dataset build, for its whole life.

The expensive stage replays real production traffic, and that traffic is
resampled on a schedule. Two candidates measured against two different samples
are not a comparison, and nothing about the numbers says so — which is why the
build a campaign pins is decided once, held against rebuilds, and checked
against what every result reports it actually replayed.
"""

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.control.launch import preflight as pf
from app.control.orchestrator.supervisor import Supervisor
from app.control.search import CandidateConfig
from app.datasets import pinning
from app.datasets.profiles import BuildInFlight, DatasetBuild, same_dataset
from app.db.base import Base
from app.db.models import (
    BaselineStatus,
    Campaign,
    CampaignStatus,
    Candidate,
    CandidateKind,
    CandidateStatus,
    Event,
    Machine,
    MachineState,
    Result,
    Run,
    RunStatus,
    User,
)
from tests.fakes import NullDriver

PROFILE = "prod-traffic-sample"

# The real thing, from the profile the platform manages for us. The full hash
# is 64 characters; a submission reports the first 16.
BUILD_A = {
    "build_id": "20260805T091004Z",
    "sha256": "db33e780d801471cdf5919570f21d692c57dd2f0485baac353b5330a7fa4be9f",
    "records": 779,
    "built_at": "2026-08-05T09:10:45Z",
    "window_start": "2026-08-04T09:00:00Z",
    "window_end": "2026-08-05T09:00:00Z",
}
BUILD_B = {**BUILD_A, "build_id": "20260806T091004Z", "sha256": "aa11" * 16, "records": 812}

SCREEN_OBJECTIVE = {"target_metric": "perf_guidellm_sweep.output_tpm_card_norm"}
REPLAY_OBJECTIVE = {"target_metric": "replay_prod.score_card_norm"}


class FakeProfiles:
    """The dataset-profile half of LLMBench, with a build that takes a tick."""

    def __init__(self, current=None, ticks_to_build=1, build_status="ready", build=None):
        self._current = current
        self.ticks_to_build = ticks_to_build
        self.build_status = build_status
        self._build = build or BUILD_A
        self.triggered = 0
        self.in_flight = False   # make trigger_build answer 409
        self.rows_seen: list[int] = []

    def find(self, name):
        return {"id": 2, "name": name, "current": self._current} if name == PROFILE else None

    def current(self, profile):
        return DatasetBuild.from_api((profile or {}).get("current") or {})

    def trigger_build(self, profile_id):
        if self.in_flight:
            raise BuildInFlight("already building")
        self.triggered += 1
        return 42

    def latest_build_row(self, profile_id):
        return 99

    def build(self, row):
        self.rows_seen.append(row)
        self.ticks_to_build -= 1
        if self.ticks_to_build > 0:
            return {"status": "running", "progress": "slice 3/24"}
        if self.build_status != "ready":
            return {"status": self.build_status, "error": "too few records collected"}
        # A published build also flips what the profile now serves.
        self._current = self._build
        return {"status": "ready", **self._build}


def _stack(*, profile=PROFILE, policy=pinning.REBUILD_AT_START, profiles=None, **kw):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add(User(id=1, username="u", password_hash="x"))
        session.add(
            Machine(id=1, name="node-24", host="10.0.0.1", gpu_count=8,
                    state=MachineState.AVAILABLE.value,
                    baseline_status=BaselineStatus.CLEARED.value)
        )
        session.add(
            Campaign(id=1, owner_id=1, name="c", engine="sglang", image="img",
                     model_path="/m", served_model_name="m",
                     search_space={"grid": {"tp": [2]}},
                     objective=SCREEN_OBJECTIVE,
                     benchmark_slug="autotune-test-v0",
                     status=CampaignStatus.ACTIVE.value,
                     run_baseline_canary=False,
                     verify_benchmark_slug="replay-autotune-v0",
                     verify_top_k=1,
                     verify_objective=REPLAY_OBJECTIVE,
                     dataset_profile=profile,
                     dataset_policy=policy,
                     **kw)
        )
        session.commit()
    supervisor = Supervisor(
        session_factory=factory, dataset_profiles=profiles or FakeProfiles()
    )
    supervisor.driver = NullDriver()
    return supervisor, factory


def _campaign(factory, campaign_id=1):
    with factory() as session:
        return session.get(Campaign, campaign_id)


def _events(factory, kind):
    with factory() as session:
        return session.scalars(select(Event).where(Event.kind == kind)).all()


def _screened(session, run_id, candidate_id, config, value):
    session.add(Candidate(id=candidate_id, campaign_id=1, config=config,
                          config_hash=CandidateConfig(engine_args=config).hash,
                          status=CandidateStatus.EXHAUSTED.value))
    session.add(Run(id=run_id, campaign_id=1, candidate_id=candidate_id,
                    machine_id=1, status=RunStatus.SUCCEEDED.value))
    session.add(Result(run_id=run_id, source="llmbench", passed=True,
                       metrics={"perf_guidellm_sweep.output_tpm_card_norm": value},
                       objective_value=value, feasible=True))


# -- deciding which build to use ----------------------------------------------


def test_a_campaign_asks_for_a_fresh_build_and_pins_what_it_gets():
    profiles = FakeProfiles(current=None, ticks_to_build=1)
    supervisor, factory = _stack(profiles=profiles)

    supervisor.tick()
    waiting = _campaign(factory)
    assert waiting.dataset_build_row == 42, "the row to poll is remembered across ticks"
    assert not waiting.dataset_build_id

    supervisor.tick()
    pinned = _campaign(factory)
    assert pinned.dataset_build_id == BUILD_A["build_id"]
    assert pinned.dataset_sha256 == BUILD_A["sha256"]
    assert pinned.dataset_policy_applied == pinning.APPLIED_REBUILT
    assert pinned.dataset_build_row == 0
    assert profiles.triggered == 1, "one build asked for, not one per tick"


def _single_stage_stack(profiles):
    """A campaign whose ONE benchmark is the replay — no separate screen. Its
    every run needs the pinned dataset, so scheduling must wait for it."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add(User(id=1, username="u", password_hash="x"))
        session.add(
            Machine(id=1, name="node-24", host="10.0.0.1", gpu_count=8,
                    state=MachineState.AVAILABLE.value,
                    baseline_status=BaselineStatus.CLEARED.value)
        )
        session.add(
            Campaign(id=1, owner_id=1, name="c", engine="sglang", image="img",
                     model_path="/m", served_model_name="m",
                     search_space={"grid": {"tp": [2]}},
                     objective=REPLAY_OBJECTIVE,
                     benchmark_slug="replay-autotune-v0",
                     status=CampaignStatus.ACTIVE.value,
                     run_baseline_canary=False,
                     dataset_profile=PROFILE,
                     dataset_policy=pinning.REBUILD_AT_START)  # no verify_* == single stage
        )
        session.commit()
    supervisor = Supervisor(session_factory=factory, dataset_profiles=profiles)
    supervisor.driver = NullDriver()
    return supervisor, factory


def test_a_single_replay_benchmark_holds_every_run_until_the_dataset_pins():
    profiles = FakeProfiles(current=None, ticks_to_build=1)
    sup, factory = _single_stage_stack(profiles)

    sup.tick()  # build triggered, still in flight
    with factory() as session:
        assert not pinning.is_pinned(session.get(Campaign, 1))
        # The planner has proposed, but nothing may run yet: a replay against an
        # unpinned dataset would not be comparable to the runs that follow.
        assert session.scalars(select(Candidate)).first() is not None
        assert session.scalars(select(Run)).first() is None

    sup.tick()  # build ready -> pinned; scheduling is now free to start
    with factory() as session:
        assert pinning.is_pinned(session.get(Campaign, 1))
        assert session.scalars(select(Run)).first() is not None


def test_use_current_takes_what_is_published_without_rebuilding():
    profiles = FakeProfiles(current=BUILD_A)
    supervisor, factory = _stack(policy=pinning.USE_CURRENT, profiles=profiles)

    supervisor.tick()

    campaign = _campaign(factory)
    assert campaign.dataset_build_id == BUILD_A["build_id"]
    assert campaign.dataset_policy_applied == pinning.APPLIED_CURRENT
    assert profiles.triggered == 0


def test_use_current_still_builds_when_nothing_has_ever_been_published():
    """A profile with no build cannot serve one, whatever the policy says —
    and the alternative is a replay that fails at submit with "no published
    build"."""
    profiles = FakeProfiles(current=None)
    supervisor, factory = _stack(policy=pinning.USE_CURRENT, profiles=profiles)

    supervisor.tick()

    assert profiles.triggered == 1


def test_a_second_campaign_adopts_the_build_the_first_is_holding():
    profiles = FakeProfiles(current=BUILD_A)
    supervisor, factory = _stack(profiles=profiles)
    supervisor.tick()  # asks for a fresh build
    supervisor.tick()  # it lands, campaign 1 pins it

    with factory() as session:
        first = session.get(Campaign, 1)
        session.add(
            Campaign(id=2, owner_id=1, name="c2", engine="sglang", image="img",
                     model_path="/m", served_model_name="m",
                     search_space={"grid": {"tp": [2]}}, objective=SCREEN_OBJECTIVE,
                     status=CampaignStatus.ACTIVE.value, run_baseline_canary=False,
                     verify_benchmark_slug="replay-autotune-v0", verify_top_k=1,
                     verify_objective=REPLAY_OBJECTIVE,
                     dataset_profile=PROFILE, dataset_policy=pinning.REBUILD_AT_START)
        )
        session.commit()
        held = first.dataset_build_id
    triggered_before = profiles.triggered

    supervisor.tick()

    with factory() as session:
        second = session.get(Campaign, 2)
    assert second.dataset_build_id == held, "same instrument, so the two compare"
    assert second.dataset_policy_applied == pinning.APPLIED_ADOPTED
    assert profiles.triggered == triggered_before, "asked to rebuild; adopted instead"


def test_a_finished_campaign_stops_holding_its_build():
    profiles = FakeProfiles(current=BUILD_A, build=BUILD_B)
    supervisor, factory = _stack(profiles=profiles)
    supervisor.tick()
    supervisor.tick()

    with factory() as session:
        first = session.get(Campaign, 1)
        first.status = CampaignStatus.DONE.value
        session.add(
            Campaign(id=2, owner_id=1, name="c2", engine="sglang", image="img",
                     model_path="/m", served_model_name="m",
                     search_space={"grid": {"tp": [2]}}, objective=SCREEN_OBJECTIVE,
                     status=CampaignStatus.ACTIVE.value, run_baseline_canary=False,
                     verify_benchmark_slug="replay-autotune-v0", verify_top_k=1,
                     verify_objective=REPLAY_OBJECTIVE,
                     dataset_profile=PROFILE, dataset_policy=pinning.REBUILD_AT_START)
        )
        session.commit()

    supervisor.tick()  # nothing holds the current build any more, so: rebuild
    supervisor.tick()  # and pin what that produced

    with factory() as session:
        second = session.get(Campaign, 2)
    assert second.dataset_policy_applied == pinning.APPLIED_REBUILT
    assert second.dataset_build_id == BUILD_B["build_id"]


def test_a_paused_campaign_still_holds_its_build():
    """Pausing means "back later", and resuming onto a swapped dataset is the
    silent break this whole mechanism exists to prevent."""
    supervisor, factory = _stack(profiles=FakeProfiles(current=BUILD_A))
    supervisor.tick()
    supervisor.tick()

    with factory() as session:
        campaign = session.get(Campaign, 1)
        campaign.status = CampaignStatus.PAUSED.value
        session.commit()
        held = campaign.dataset_build_id
        assert held
        assert [c.id for c in pinning.holders(session, PROFILE, held)] == [1]


def test_a_build_already_in_flight_is_polled_rather_than_asked_for_again():
    profiles = FakeProfiles(current=None, ticks_to_build=1)
    profiles.in_flight = True
    supervisor, factory = _stack(profiles=profiles)

    supervisor.tick()

    assert _campaign(factory).dataset_build_row == 99, "somebody else's row, now ours to watch"
    assert profiles.triggered == 0


# -- when the dataset does not arrive -----------------------------------------


def test_a_refused_build_falls_back_to_whatever_is_still_published():
    """A failed build leaves the previous one serving — older than asked for,
    but consistent, which is the property that matters."""
    profiles = FakeProfiles(current=BUILD_A, build_status="failed")
    supervisor, factory = _stack(profiles=profiles)

    supervisor.tick()
    supervisor.tick()

    campaign = _campaign(factory)
    assert campaign.dataset_build_id == BUILD_A["build_id"]
    assert campaign.dataset_policy_applied == pinning.APPLIED_CURRENT
    assert _events(factory, "dataset_build_failed")


def test_a_profile_that_does_not_exist_is_concluded_once_not_retried_forever():
    supervisor, factory = _stack(profile="typo-profile-v0")

    supervisor.tick()
    supervisor.tick()
    supervisor.tick()

    campaign = _campaign(factory)
    assert campaign.dataset_policy_applied == pinning.APPLIED_UNAVAILABLE
    assert len(_events(factory, "dataset_unavailable")) == 1


def test_verification_waits_for_the_pin_rather_than_finishing_the_campaign():
    profiles = FakeProfiles(current=None, ticks_to_build=3)
    supervisor, factory = _stack(profiles=profiles)
    with factory() as session:
        _screened(session, 1, 1, {"tp": 2}, 68000.0)
        session.commit()

    supervisor.tick()

    with factory() as session:
        campaign = session.get(Campaign, 1)
        queued = session.scalars(
            select(Candidate).where(Candidate.kind == CandidateKind.VERIFICATION.value)
        ).all()
    assert campaign.status == CampaignStatus.ACTIVE.value, "not done — the pin is seconds away"
    assert queued == []


def test_an_unavailable_dataset_verifies_unpinned_rather_than_skipping_the_stage():
    """Falling back to what the benchmark resolves is exactly what every
    campaign did before pinning existed — no worse, and an hour of evidence
    beats a campaign that sat out its window waiting for a dataset that had
    already said no. The event is what says the pin never landed."""
    supervisor, factory = _stack(profile="typo-profile-v0")
    with factory() as session:
        _screened(session, 1, 1, {"tp": 2}, 68000.0)
        session.commit()

    supervisor.tick()

    with factory() as session:
        queued = session.scalars(
            select(Candidate).where(Candidate.kind == CandidateKind.VERIFICATION.value)
        ).all()
    assert len(queued) == 1
    assert _events(factory, "dataset_unavailable")


# -- checking what actually got replayed --------------------------------------


def test_the_full_hash_and_the_16_character_one_a_result_reports_are_one_dataset():
    assert same_dataset(BUILD_A["sha256"], BUILD_A["sha256"][:16])
    assert not same_dataset(BUILD_A["sha256"], BUILD_B["sha256"][:16])
    assert not same_dataset("", BUILD_A["sha256"])


def test_a_result_from_another_build_is_flagged_and_ranked_last():
    campaign = Campaign(
        dataset_profile=PROFILE,
        dataset_build_id=BUILD_A["build_id"],
        dataset_sha256=BUILD_A["sha256"],
    )
    same = {"replay_prod.dataset_id": BUILD_A["build_id"],
            "replay_prod.dataset_sha256": BUILD_A["sha256"][:16]}
    other = {"replay_prod.dataset_id": BUILD_B["build_id"],
             "replay_prod.dataset_sha256": BUILD_B["sha256"][:16]}

    assert pinning.mismatch(campaign, same) is None
    assert pinning.mismatch(campaign, other) == (BUILD_B["build_id"], BUILD_B["sha256"][:16])
    assert pinning.comparable(campaign, same)
    assert not pinning.comparable(campaign, other)


def test_a_screening_result_has_no_dataset_and_is_not_missing_one():
    campaign = Campaign(
        dataset_profile=PROFILE,
        dataset_build_id=BUILD_A["build_id"],
        dataset_sha256=BUILD_A["sha256"],
    )
    assert pinning.mismatch(campaign, {"perf_guidellm_sweep.output_tps": 285.0}) is None


def test_a_campaign_that_pins_nothing_flags_nothing():
    assert pinning.mismatch(Campaign(), {"replay_prod.dataset_id": "whatever"}) is None


# -- the check that runs before the night is spent ----------------------------


def test_preflight_fails_when_the_benchmark_replays_a_different_profile():
    check = pf.dataset_check(
        PROFILE, "replay-autotune-v0",
        {"replay-autotune-v0": "qwen36-daily-test-0"},
        [PROFILE, "qwen36-daily-test-0"],
    )
    assert check.status == pf.FAIL
    assert "qwen36-daily-test-0" in check.detail


def test_preflight_fails_when_the_benchmark_resolves_no_profile_at_all():
    check = pf.dataset_check(PROFILE, "replay-autotune-v0", {"replay-autotune-v0": ""}, [PROFILE])
    assert check.status == pf.FAIL
    assert "changes nothing" in check.detail


def test_preflight_warns_when_a_rolling_dataset_is_used_unpinned():
    check = pf.dataset_check(
        "", "rolling-replay-test-mf-v0",
        {"rolling-replay-test-mf-v0": "qwen36-daily-test-0"}, None,
    )
    assert check.status == pf.WARN


def test_preflight_passes_when_the_wiring_matches():
    check = pf.dataset_check(
        PROFILE, "replay-autotune-v0", {"replay-autotune-v0": PROFILE}, [PROFILE]
    )
    assert check.status == pf.PASS


def test_preflight_skips_rather_than_blocking_when_llmbench_cannot_be_asked():
    check = pf.dataset_check(PROFILE, "replay-autotune-v0", None, None)
    assert check.status == pf.SKIP
