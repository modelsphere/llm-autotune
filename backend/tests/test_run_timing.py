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
