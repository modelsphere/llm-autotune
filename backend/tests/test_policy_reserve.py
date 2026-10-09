"""The validation reserve — how much of the window is held back from search so
the best contenders can actually be measured before the hard cutoff.

The reserve is the only thing standing between "found a great config" and
"recorded a verdict for it", so its arithmetic is worth pinning: a contender
costs its benchmark plus its model's startup, learned from the campaign's own
runs unless the campaign pinned either; an explicit override wins outright.
"""

from app.control.orchestrator.policy_lifecycle import (
    FALLBACK_CONTENDER_MINUTES,
    contender_minutes,
    contender_parts,
)
from app.db.models import Campaign
from app.schemas.policy import PolicySettings


def _campaign(settings: dict | None = None, timing: dict | None = None,
              verify_slug: str = "") -> Campaign:
    return Campaign(policy_settings=settings or {}, run_timing=timing or {},
                    verify_benchmark_slug=verify_slug)


def test_reserve_is_per_contender_time_plus_margin():
    s = PolicySettings(max_contenders=2)
    # 2 * 60 + 15
    assert s.reserve_minutes(60) == 135


def test_explicit_override_ignores_the_per_contender_math():
    s = PolicySettings(max_contenders=4, validation_reserve_minutes=90)
    assert s.reserve_minutes(75) == 90


def test_nothing_measured_yet_falls_back():
    assert contender_minutes(_campaign()) == FALLBACK_CONTENDER_MINUTES


def test_learned_from_the_campaigns_runs():
    # Startup 10 min and benchmarks of 20 min, each with 20% headroom.
    campaign = _campaign(timing={"startup": [8, 10], "bench_screen": [18, 20]})
    assert contender_parts(campaign) == (24, 12)
    assert contender_minutes(campaign) == 36


def test_slow_runs_set_the_estimate_not_the_average():
    # The 90th percentile: two slow runs in ten set the estimate; a single
    # one (the first run pulling its image cold) does not.
    campaign = _campaign(timing={"bench_screen": [10] * 8 + [40, 40]})
    assert contender_parts(campaign)[0] == 48
    campaign.run_timing = {"bench_screen": [40] + [10] * 9}
    assert contender_parts(campaign)[0] == 12


def test_a_second_benchmark_is_not_estimated_from_the_first():
    # Validation runs the verify suite; a screen sweep says nothing about how
    # long a replay takes, so this falls back until a verify run is measured.
    staged = _campaign(timing={"bench_screen": [5]}, verify_slug="replay-1k")
    assert contender_minutes(staged) == FALLBACK_CONTENDER_MINUTES
    staged.run_timing = {"bench_screen": [5], "bench_verify": [50]}
    assert contender_parts(staged)[0] == 60


def test_pinned_values_win_over_learned_ones():
    campaign = _campaign(
        settings={"approx_minutes_each": 30, "model_startup_minutes": 0},
        timing={"startup": [10], "bench_screen": [20]},
    )
    assert contender_parts(campaign) == (30, 0)
