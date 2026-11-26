"""A latency campaign (Part 1 of the replay test plan): a timed replay
workload, the "1x replay: lowest latency" objective, its guardrails relative
to production, the baseline measured twice, and gains within that noise."""

from types import SimpleNamespace

from app.db.bootstrap import BUILTIN_OBJECTIVES
from app.evaluation.benchmark_spec import BenchmarkSpec, document
from app.evaluation.ranking import leaderboard_entries, winner
from app.objective import baseline_repeats, is_timed
from app.quality import resolve

LATENCY = next(o for o in BUILTIN_OBJECTIVES if o["name"] == "1x replay: lowest latency")
EQUIVALENT = next(o for o in BUILTIN_OBJECTIVES if o["name"] == "1x replay: equivalent throughput")


# --------------------------------------------------------------- the workload

def test_a_timed_replay_is_its_own_benchmark():
    closed = BenchmarkSpec(kind="replay", dataset_profile="p")
    timed = BenchmarkSpec(kind="replay", dataset_profile="p", schedule="timed")
    assert closed.slug != timed.slug
    # requests and concurrency do not apply to a timed replay
    assert timed.slug == BenchmarkSpec(kind="replay", dataset_profile="p", schedule="timed",
                                       requests=7, concurrency=3).slug
    assert timed.slug != BenchmarkSpec(kind="replay", dataset_profile="p", schedule="timed",
                                       warmup_s=60).slug
    assert "recorded timing" in timed.title


def test_a_closed_replay_keeps_its_slug():
    # essentials() only grows for timed specs: every slug made before stays.
    spec = BenchmarkSpec(kind="replay", dataset_profile="p")
    assert "schedule" not in spec.essentials()


def test_the_timed_document_asks_llmbench_for_the_timed_schedule():
    doc = document(BenchmarkSpec(kind="replay", dataset_profile="p", schedule="timed",
                                 warmup_s=45))
    params = doc["modules"][0]["params"]
    assert params["schedule"] == "timed" and params["warmup_s"] == 45
    assert params["dataset_source"] == "auto" and params["dataset_profile"] == "p"
    assert "concurrency" not in params and "max_samples" not in params


# --------------------------------------------------------------- the objective

def test_the_latency_objective_minimizes_median_latency():
    assert LATENCY["direction"] == "minimize"
    assert is_timed(LATENCY) and baseline_repeats(LATENCY) == 2
    assert baseline_repeats({"target_metric": "replay.output_tpm_card_norm"}) == 1


def test_the_latency_objective_judges_only_the_schedule():
    # The p90 guardrails are gone: a 2 % limit on a tail that moves 13 % between
    # two runs of the same config dropped configs at random.
    assert [r["metric"] for r in LATENCY["redlines"]] == ["replay.fell_behind"]


def test_the_equivalent_throughput_objective_is_a_timed_one():
    # Ranked on what a timed replay reports, so it needs one, and production
    # is measured twice for its noise floor as in a latency campaign.
    assert EQUIVALENT["target_metric"] == "replay.equivalent_tpm"
    assert EQUIVALENT["direction"] == "maximize"
    assert is_timed(EQUIVALENT) and baseline_repeats(EQUIVALENT) == 2
    # failed requests leave the curve, so a config that drops them is out
    assert {r["metric"] for r in EQUIVALENT["redlines"]} == {"replay.fell_behind", "replay.uptime"}


def test_a_relative_redline_can_be_a_multiple_of_production():
    objective = {"redlines": [{"metric": "replay.e2e_p90_ms", "op": "<=",
                               "relative_to": "production", "factor": 1.2}]}
    r = resolve(objective, {"replay.e2e_p90_ms": 10000.0})["redlines"][0]
    assert r["value"] == 12000.0 and "relative_to" not in r and "× 1.2" in r["basis"]


def test_an_offset_floor_still_resolves_as_before():
    objective = {"redlines": [{"metric": "q", "op": ">=", "relative_to": "production",
                               "value": -0.1}]}
    assert resolve(objective, {"q": 0.7})["redlines"][0]["value"] == 0.6


# --------------------------------------------------------------- noise

def _campaign(objective):
    return SimpleNamespace(id=1, objective=objective, verify_objective=None,
                           verify_benchmark_slug="", recipe=None, policy_id=None,
                           search_space={}, dataset_profile="",
                           dataset_policy="")


def _row(i, value, baseline=False):
    run = SimpleNamespace(id=i, env_snapshot={"card_type": "H100"})
    candidate = SimpleNamespace(config={"n": i}, is_baseline=baseline, kind="search")
    result = SimpleNamespace(score=None, metrics={}, raw=None, objective_value=value,
                             feasible=True, constraints=[], breaches=[])
    return run, candidate, result


def test_a_gain_within_the_baselines_spread_is_no_winner():
    # production measured at 1000 and 1040 ms: 4 % noise
    rows = [_row(1, 1000.0, True), _row(2, 1040.0, True), _row(3, 1010.0)]
    entries = leaderboard_entries(_campaign(LATENCY), rows)
    cand = next(e for e in entries if not e.is_baseline)
    assert cand.noise and abs(cand.noise - 0.0392) < 0.001
    assert cand.within_noise is True
    assert winner(entries) is None


def test_a_gain_beyond_the_noise_wins():
    rows = [_row(1, 1000.0, True), _row(2, 1020.0, True), _row(3, 900.0), _row(4, 1005.0)]
    entries = leaderboard_entries(_campaign(LATENCY), rows)
    top = winner(entries)
    assert top is not None and top.run_id == 3 and top.within_noise is False
    assert top.vs_baseline < 1                          # lower latency than production


def test_an_equivalent_throughput_gain_within_the_noise_is_no_winner():
    # production measured at 100k and 104k tok/min; the higher score wins
    rows = [_row(1, 100_000.0, True), _row(2, 104_000.0, True), _row(3, 103_000.0)]
    assert winner(leaderboard_entries(_campaign(EQUIVALENT), rows)) is None
    rows.append(_row(4, 115_000.0))
    top = winner(leaderboard_entries(_campaign(EQUIVALENT), rows))
    assert top is not None and top.run_id == 4 and top.vs_baseline > 1


def test_throughput_campaigns_get_no_noise_verdict():
    tput = {"target_metric": "replay.output_tpm_card_norm", "direction": "maximize"}
    rows = [_row(1, 1000.0, True), _row(2, 1040.0, True), _row(3, 1010.0)]
    entries = leaderboard_entries(_campaign(tput), rows)
    assert all(e.within_noise is None for e in entries)
    assert winner(entries).run_id == 3


# --------------------------------------------------------------- baseline twice

def _supervisor_with(objective):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.control.orchestrator.supervisor import Supervisor
    from app.db.base import Base
    from app.db.models import Campaign, CampaignStatus, User
    from tests.fakes import NullDriver

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as s:
        s.add(User(id=1, username="a", password_hash="x", role="admin"))
        s.add(Campaign(id=1, owner_id=1, name="c", engine="sglang", image="img",
                       model_path="/m", served_model_name="m", search_space={},
                       objective=objective, status=CampaignStatus.ACTIVE.value,
                       run_baseline_canary=True))
        s.commit()
    sup = Supervisor(session_factory=factory)
    sup.driver = NullDriver()
    sup._campaign_baseline = lambda session, campaign, machine=None: SimpleNamespace(
        id=7, engine_args={"tp": 8})
    return sup, factory


def _baselines(factory):
    from sqlalchemy import select

    from app.db.models import Candidate
    with factory() as s:
        return [c.config_hash for c in s.scalars(select(Candidate)) if c.is_baseline]


def test_a_latency_campaign_measures_production_twice_and_only_twice():
    from app.db.models import Campaign
    sup, factory = _supervisor_with(LATENCY)
    for _ in range(2):
        with factory() as s:
            sup._ensure_relaunch_baseline(s, s.get(Campaign, 1))
            s.commit()
    assert sorted(_baselines(factory)) == ["baseline-search-7", "baseline-search-7-2"]


def test_an_equivalent_throughput_campaign_measures_production_twice():
    from app.db.models import Campaign
    sup, factory = _supervisor_with(EQUIVALENT)
    for _ in range(2):
        with factory() as s:
            sup._ensure_relaunch_baseline(s, s.get(Campaign, 1))
            s.commit()
    assert len(_baselines(factory)) == 2


def test_a_throughput_campaign_measures_it_once():
    from app.db.models import Campaign
    sup, factory = _supervisor_with({"target_metric": "replay.output_tpm_card_norm"})
    with factory() as s:
        sup._ensure_relaunch_baseline(s, s.get(Campaign, 1))
        s.commit()
    assert _baselines(factory) == ["baseline-search-7"]
