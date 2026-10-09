"""How much window one more run needs: learned, capped, never the cap alone.

The per-run cap is a safety bound, set high (12 hours) so it never limits a
policy. Reserving the cap itself before every run would stop a 9-hour window
from starting anything, so the reservation is what the campaign's runs have
taken — and before the first one, the platform default.
"""

from datetime import UTC, datetime, timedelta

from app.control.orchestrator.lifecycle import window_allows_new_run
from app.control.orchestrator.timing import window_minutes
from app.db.models import Campaign
from app.staging import SCREEN, VERIFY


def _campaign(timing: dict | None = None, **window) -> Campaign:
    return Campaign(max_run_minutes=720, run_timing=timing or {}, **window)


def test_before_any_run_the_default_is_reserved_not_the_cap():
    assert window_minutes(_campaign(), SCREEN, 720, 150) == 150


def test_after_runs_the_learned_length_is_reserved():
    # 10 min to come up, 20 to measure, 20% headroom.
    campaign = _campaign({"startup": [10], "bench_screen": [20]})
    assert window_minutes(campaign, SCREEN, 720, 150) == 36


def test_the_cap_still_bounds_the_estimate():
    campaign = _campaign({"bench_screen": [200]})
    assert window_minutes(campaign, SCREEN, 120, 150) == 120


def test_each_stage_learns_separately():
    campaign = _campaign({"bench_screen": [10], "bench_verify": [50]})
    assert window_minutes(campaign, SCREEN, 720, 150) == 12
    assert window_minutes(campaign, VERIFY, 180, 150) == 60


def test_a_twelve_hour_cap_still_starts_runs_in_a_nine_hour_window():
    start = datetime.now(UTC) - timedelta(minutes=5)
    campaign = _campaign(window_start=start, window_end=start + timedelta(hours=9))
    assert window_allows_new_run(campaign, 150)


# -- before the first run: an estimate from the benchmark, then a cut run ------

from app.control.orchestrator import timing  # noqa: E402

SWEEP = {"modules": [{"module_name": "perf_guidellm_sweep",
                      "params_json": {"search_mode": "grid", "concurrencies": "1,4,16,64",
                                      "max_seconds": 60, "warmup_seconds": 10}}]}


def test_a_sweep_is_estimated_from_its_levels():
    # 4 levels × (60 + 10 + 30 overhead) s = 400 s
    assert timing.estimate_benchmark_minutes(SWEEP) == 400 / 60


def test_a_replay_is_estimated_only_from_a_real_time_cap():
    capped = {"modules": [{"module_name": "replay", "params_json": {"max_seconds": 1800}}]}
    assert timing.estimate_benchmark_minutes(capped) == 30
    default_cap = {"modules": [{"module_name": "replay", "params_json": {"max_seconds": 18000}}]}
    assert timing.estimate_benchmark_minutes(default_cap) is None
    unknown = {"modules": [{"module_name": "some_suite", "params_json": {}}]}
    assert timing.estimate_benchmark_minutes(unknown) is None


def test_the_estimate_plans_the_first_run_with_the_startup_bound():
    campaign = _campaign()
    timing.set_priors(campaign, {SCREEN: 20.0})
    # (20 benchmark + 30 readiness bound) × 1.2
    assert window_minutes(campaign, SCREEN, 720, 240) == 60


def test_a_measurement_replaces_the_estimate():
    campaign = _campaign()
    timing.set_priors(campaign, {SCREEN: 20.0})
    campaign.run_timing = {**campaign.run_timing, "startup": [5], "bench_screen": [10]}
    assert window_minutes(campaign, SCREEN, 720, 240) == 18


def test_nothing_known_falls_back_to_the_platform_default():
    assert window_minutes(_campaign(), SCREEN, 720, 240) == 240


def test_a_run_cut_at_the_window_end_still_teaches_its_length():
    """A run cut 40 minutes into its benchmark took at least that long, so the
    next night reserves at least that much instead of cutting it again."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.base import Base
    from app.db.models import Candidate, Event, Run, RunKind, User

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(engine)() as session:
        session.add(User(id=1, username="u", password_hash="x"))
        campaign = Campaign(id=1, owner_id=1, name="c", engine="sglang", image="i",
                            model_path="/m", served_model_name="m", search_space={},
                            max_run_minutes=720, run_timing={})
        session.add(campaign)
        session.add(Candidate(id=1, campaign_id=1, config={}, config_hash="h"))
        now = datetime.now(UTC)
        run = Run(id=1, campaign_id=1, candidate_id=1, kind=RunKind.EXPERIMENT.value,
                  status="benching", started_at=now - timedelta(minutes=50))
        session.add(run)
        session.add(Event(actor="worker", kind="run_transition", run_id=1,
                          payload={"to": "benching"}, ts=now - timedelta(minutes=40)))
        session.flush()
        timing.record_cut(session, run, SCREEN, now)
        assert round(timing.bench_minutes(campaign)) == 40
