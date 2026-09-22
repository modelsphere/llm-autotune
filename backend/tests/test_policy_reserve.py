"""The validation reserve — how much of the window is held back from search so
the best contenders can actually be measured before the hard cutoff.

The reserve is the only thing standing between "found a great config" and
"recorded a verdict for it", so its arithmetic is worth pinning: a slow-loading
model must reserve its cold-start per contender, and an explicit override must
win outright.
"""

from app.schemas.policy import PolicySettings


def test_default_reserve_is_bench_time_per_contender_plus_margin():
    s = PolicySettings(max_contenders=2, approx_minutes_each=60)
    # 2 * (60 + 0) + 15
    assert s.reserve_minutes() == 135


def test_model_startup_is_reserved_per_contender():
    # A 15-minute cold start, three contenders: the load is budgeted for every
    # one of them, not once.
    s = PolicySettings(max_contenders=3, approx_minutes_each=20, model_startup_minutes=15)
    # 3 * (20 + 15) + 15
    assert s.reserve_minutes() == 120


def test_startup_defaults_to_zero_so_old_campaigns_are_unchanged():
    s = PolicySettings(max_contenders=2, approx_minutes_each=60)
    assert s.model_startup_minutes == 0
    assert s.reserve_minutes() == 135


def test_explicit_override_ignores_the_startup_math():
    s = PolicySettings(
        max_contenders=4, approx_minutes_each=60, model_startup_minutes=15,
        validation_reserve_minutes=90,
    )
    assert s.reserve_minutes() == 90
