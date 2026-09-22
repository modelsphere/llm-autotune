"""Nightly window arithmetic.

Almost every bug available here is an off-by-one-day: a window that crosses
midnight belongs to the day it STARTED on, and code that asks "what is today's
window" at 02:00 gets the wrong answer unless it also looks at yesterday.
"""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.control.orchestrator import schedule as sched

SHANGHAI = ZoneInfo("Asia/Shanghai")
NIGHTLY = sched.Schedule(start=time(23, 0), end=time(8, 0), timezone="Asia/Shanghai")
DAYTIME = sched.Schedule(start=time(9, 0), end=time(17, 0), timezone="Asia/Shanghai")


def _local(year, month, day, hour, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=SHANGHAI).astimezone(UTC)


# -- an overnight window ------------------------------------------------------


def test_the_window_is_open_just_after_it_starts():
    window = sched.current_window(_local(2026, 8, 4, 23, 1), NIGHTLY)
    assert window is not None
    start, end = window
    assert start == _local(2026, 8, 4, 23, 0)
    assert end == _local(2026, 8, 5, 8, 0)


def test_the_window_is_still_open_after_midnight():
    """02:00 belongs to the window that opened at 23:00 YESTERDAY. Looking only
    at today's occurrence ends the night four hours early."""
    window = sched.current_window(_local(2026, 8, 5, 2, 0), NIGHTLY)
    assert window is not None
    assert window[0] == _local(2026, 8, 4, 23, 0)


def test_the_window_is_shut_during_the_day():
    assert sched.current_window(_local(2026, 8, 5, 12, 0), NIGHTLY) is None


def test_the_end_instant_is_outside_the_window():
    """08:00 sharp is closed. A half-open interval is what stops one tick
    seeing the window as both ending and still running."""
    assert sched.current_window(_local(2026, 8, 5, 8, 0), NIGHTLY) is None
    assert sched.current_window(_local(2026, 8, 5, 7, 59), NIGHTLY) is not None


def test_the_next_window_is_tonight_when_asked_during_the_day():
    upcoming = sched.next_window(_local(2026, 8, 5, 12, 0), NIGHTLY)
    assert upcoming is not None
    assert upcoming[0] == _local(2026, 8, 5, 23, 0)


def test_the_next_window_is_tomorrow_when_asked_mid_window():
    upcoming = sched.next_window(_local(2026, 8, 5, 2, 0), NIGHTLY)
    assert upcoming is not None
    assert upcoming[0] == _local(2026, 8, 5, 23, 0)


# -- a same-day window --------------------------------------------------------


def test_a_daytime_window_does_not_cross_midnight():
    assert not DAYTIME.crosses_midnight
    window = sched.current_window(_local(2026, 8, 5, 10, 0), DAYTIME)
    assert window is not None
    assert window[1] == _local(2026, 8, 5, 17, 0)


def test_a_daytime_window_is_shut_at_night():
    assert sched.current_window(_local(2026, 8, 5, 2, 0), DAYTIME) is None


# -- the stop date ------------------------------------------------------------


def test_no_window_opens_after_the_until_date():
    limited = sched.Schedule(
        start=time(23, 0), end=time(8, 0), timezone="Asia/Shanghai",
        until=_local(2026, 8, 4, 12, 0),
    )
    assert sched.next_window(_local(2026, 8, 4, 13, 0), limited) is None
    assert sched.is_finished(_local(2026, 8, 4, 13, 0), limited)


def test_a_window_already_open_is_not_cut_short_by_the_until_date():
    """`until` stops new occurrences starting; it does not evict a night that
    is already running. Ending mid-benchmark to satisfy a date is a surprise
    nobody asked for."""
    limited = sched.Schedule(
        start=time(23, 0), end=time(8, 0), timezone="Asia/Shanghai",
        until=_local(2026, 8, 5, 3, 0),
    )
    assert sched.current_window(_local(2026, 8, 5, 2, 0), limited) is not None


# -- parsing and refusal ------------------------------------------------------


def test_times_round_trip_through_text():
    assert sched.parse_hhmm("23:00") == time(23, 0)
    assert sched.parse_hhmm("08:30") == time(8, 30)
    assert sched.format_hhmm(time(8, 30)) == "08:30"


def test_nonsense_times_are_none_rather_than_an_exception():
    """A malformed schedule must leave one campaign asleep, not raise inside
    the tick loop that every other campaign shares."""
    for bad in ("", "25:00", "8", "abc", "12:60", None, "12:00:00"):
        assert sched.parse_hhmm(bad) is None


def test_a_half_written_schedule_is_refused():
    assert sched.errors("23:00", "", "") == [
        "a nightly schedule needs both a start and an end time"
    ]


def test_a_zero_length_window_is_refused():
    problems = sched.errors("23:00", "23:00", "")
    assert any("same" in p for p in problems)


def test_an_unknown_timezone_is_refused():
    assert any("timezone" in p for p in sched.errors("23:00", "08:00", "Mars/Olympus"))


def test_no_schedule_at_all_is_not_an_error():
    assert sched.errors("", "", "") == []


# -- reading a campaign -------------------------------------------------------


class _Campaign:
    def __init__(self, **kw):
        self.daily_start = kw.get("daily_start", "")
        self.daily_end = kw.get("daily_end", "")
        self.schedule_timezone = kw.get("schedule_timezone", "")
        self.schedule_until = kw.get("schedule_until")


def test_a_campaign_without_times_has_no_schedule():
    assert sched.from_campaign(_Campaign()) is None


def test_a_campaign_with_times_gets_the_default_timezone():
    schedule = sched.from_campaign(_Campaign(daily_start="23:00", daily_end="08:00"))
    assert schedule is not None
    assert schedule.timezone == sched.DEFAULT_TIMEZONE


def test_a_naive_until_is_read_as_utc():
    """Postgres hands back aware datetimes and sqlite naive ones; comparing the
    two raises, so the boundary normalizes rather than trusting the driver."""
    schedule = sched.from_campaign(
        _Campaign(daily_start="23:00", daily_end="08:00",
                  schedule_until=datetime(2026, 8, 4, 12, 0))
    )
    assert schedule is not None and schedule.until.tzinfo is not None


# -- the property the whole design rests on -----------------------------------


def test_windows_never_overlap_and_repeat_every_day():
    """Walked hour by hour across a week.

    Every instant belongs to at most one occurrence, each occurrence is exactly
    nine hours, and consecutive occurrences start exactly a day apart — which
    is the whole claim "nightly" makes. Counting the windows instead would only
    have tested my arithmetic about where the walk begins.
    """
    moment = _local(2026, 8, 1, 0, 0)
    seen: set[datetime] = set()
    for _ in range(24 * 7):
        window = sched.current_window(moment, NIGHTLY)
        if window is not None:
            start, end = window
            assert start <= moment < end
            assert end - start == timedelta(hours=9)
            seen.add(start)
        moment += timedelta(hours=1)

    starts = sorted(seen)
    assert len(starts) >= 7
    gaps = {b - a for a, b in zip(starts, starts[1:], strict=False)}
    assert gaps == {timedelta(days=1)}, f"nightly means one day apart, got {gaps}"


def test_a_window_can_start_and_end_on_an_arbitrary_minute():
    """The editor offered half hours only, so 23:10 was not expressible at all
    — an operator wanting to clear a nightly job at 23:05 had to round to 23:30
    and give up the time. The arithmetic never cared; only the picker did."""
    odd = sched.Schedule(
        start=sched.parse_hhmm("23:10"),
        end=sched.parse_hhmm("07:45"),
        timezone="Asia/Shanghai",
    )
    assert odd.crosses_midnight

    # Just inside, at the top of the window.
    start, end = sched.current_window(_local(2026, 8, 4, 23, 10), odd)
    assert start == _local(2026, 8, 4, 23, 10)
    assert end == _local(2026, 8, 5, 7, 45)
    assert end - start == timedelta(hours=8, minutes=35)

    # A minute before it opens, and a minute after it shuts.
    assert sched.current_window(_local(2026, 8, 4, 23, 9), odd) is None
    assert sched.current_window(_local(2026, 8, 5, 7, 45), odd) is None
    # And one minute before the end is still inside.
    assert sched.current_window(_local(2026, 8, 5, 7, 44), odd) is not None


def test_an_odd_minute_window_still_repeats_exactly_daily():
    odd = sched.Schedule(
        start=sched.parse_hhmm("23:07"),
        end=sched.parse_hhmm("06:53"),
        timezone="Asia/Shanghai",
    )
    moment = _local(2026, 8, 1, 0, 0)
    starts: set[datetime] = set()
    for _ in range(24 * 4):
        window = sched.current_window(moment, odd)
        if window is not None:
            starts.add(window[0])
        moment += timedelta(minutes=37)  # a stride that lands on odd minutes

    ordered = sorted(starts)
    assert len(ordered) >= 3
    gaps = {b - a for a, b in zip(ordered, ordered[1:], strict=False)}
    assert gaps == {timedelta(days=1)}, gaps
