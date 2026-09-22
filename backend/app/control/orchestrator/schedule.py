"""When a campaign is awake.

A tuning campaign is a standing arrangement, not an appointment: "have the
machine from 23:00 until 08:00, every night, until the search runs out". The
old model stored two absolute datetimes, so a nightly job meant creating a new
campaign every evening — a manual step at the exact hour nobody is awake for.

A schedule here is a time-of-day window in a named timezone, plus an optional
date to stop repeating. The supervisor asks two questions each tick:

    current_window(now, sched)  -> the occurrence containing `now`, or None
    next_window(now, sched)     -> the one after that, for "starts in 4h"

and writes the answer onto `Campaign.window_start/window_end`. Everything
downstream — the run-fits-in-the-window check, the hand-back trigger, the hard
cutoff — keeps reading those two fields and never learns that a schedule
exists. That is deliberate: a recurring campaign and a one-off one are the same
campaign to the parts of the system that spend money.

Timezone is stored per campaign because "23:00" is a fact about the operator's
night, not about UTC. A window written in Shanghai stays correct when the
platform is deployed somewhere else, and stays correct across a DST change in
timezones that have one.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Asia/Shanghai"


@dataclass(frozen=True)
class Schedule:
    """A nightly window. `start` == `end` is rejected by `errors()` rather than
    silently meaning either "always" or "never"."""

    start: time
    end: time
    timezone: str = DEFAULT_TIMEZONE
    # Stop repeating after this instant. None = until the search is exhausted.
    until: datetime | None = None

    @property
    def crosses_midnight(self) -> bool:
        return self.end <= self.start

    def zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return ZoneInfo(DEFAULT_TIMEZONE)


def now_utc() -> datetime:
    return datetime.now(UTC)


def parse_hhmm(value: Any) -> time | None:
    """"23:00" -> time(23, 0). Anything unparseable is None, not an exception:
    a malformed schedule must leave the campaign un-scheduled, not crash the
    tick loop that every other campaign shares."""
    if isinstance(value, time):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    parts = value.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return time(hour, minute)


def format_hhmm(value: time | None) -> str:
    return "" if value is None else f"{value.hour:02d}:{value.minute:02d}"


def from_campaign(campaign: Any) -> Schedule | None:
    """The schedule a campaign declares, or None if it has none."""
    start = parse_hhmm(getattr(campaign, "daily_start", None))
    end = parse_hhmm(getattr(campaign, "daily_end", None))
    if start is None or end is None or start == end:
        return None
    return Schedule(
        start=start,
        end=end,
        timezone=getattr(campaign, "schedule_timezone", "") or DEFAULT_TIMEZONE,
        until=_as_utc(getattr(campaign, "schedule_until", None)),
    )


def errors(daily_start: Any, daily_end: Any, timezone: Any) -> list[str]:
    """Mistakes worth refusing at creation rather than discovering at 23:00."""
    out: list[str] = []
    start, end = parse_hhmm(daily_start), parse_hhmm(daily_end)
    given = [v for v in (daily_start, daily_end) if str(v or "").strip()]
    if len(given) == 1:
        out.append("a nightly schedule needs both a start and an end time")
    if daily_start and start is None:
        out.append(f"daily_start '{daily_start}' is not HH:MM")
    if daily_end and end is None:
        out.append(f"daily_end '{daily_end}' is not HH:MM")
    if start is not None and end is not None and start == end:
        out.append("daily_start and daily_end are the same; a window needs a length")
    if timezone:
        try:
            ZoneInfo(str(timezone))
        except (ZoneInfoNotFoundError, ValueError):
            out.append(f"unknown timezone '{timezone}'")
    return out


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _occurrence(schedule: Schedule, day: date) -> tuple[datetime, datetime]:
    """The window that STARTS on `day`, as UTC instants.

    Built in local time and then converted, so 23:00 means 23:00 to the person
    who typed it even when the offset shifts underneath.
    """
    zone = schedule.zone()
    start = datetime.combine(day, schedule.start, tzinfo=zone)
    end_day = day + timedelta(days=1) if schedule.crosses_midnight else day
    end = datetime.combine(end_day, schedule.end, tzinfo=zone)
    return start.astimezone(UTC), end.astimezone(UTC)


def _candidate_days(schedule: Schedule, now: datetime) -> list[date]:
    """Days whose occurrence could contain or follow `now`.

    Yesterday is in the list because a window that crosses midnight is still
    running at 02:00 — it started on the previous local day, and forgetting
    that is how a night ends four hours early.
    """
    today = now.astimezone(schedule.zone()).date()
    return [today - timedelta(days=1), today, today + timedelta(days=1)]


def current_window(now: datetime, schedule: Schedule | None) -> tuple[datetime, datetime] | None:
    """The occurrence containing `now`, if the campaign should be awake."""
    if schedule is None:
        return None
    for day in _candidate_days(schedule, now):
        start, end = _occurrence(schedule, day)
        if start <= now < end:
            if schedule.until is not None and start > schedule.until:
                return None
            return start, end
    return None


def next_window(now: datetime, schedule: Schedule | None) -> tuple[datetime, datetime] | None:
    """The next occurrence starting strictly after `now`, if any remain."""
    if schedule is None:
        return None
    for day in _candidate_days(schedule, now):
        start, end = _occurrence(schedule, day)
        if start > now:
            if schedule.until is not None and start > schedule.until:
                return None
            return start, end
    return None


def is_finished(now: datetime, schedule: Schedule | None) -> bool:
    """No occurrence is running and none will start again."""
    if schedule is None:
        return False
    return current_window(now, schedule) is None and next_window(now, schedule) is None


def describe(schedule: Schedule | None) -> str:
    """One line, for a report or a log — not for the UI, which renders its own."""
    if schedule is None:
        return "no schedule"
    span = f"{format_hhmm(schedule.start)}–{format_hhmm(schedule.end)} {schedule.timezone}"
    if schedule.crosses_midnight:
        span += " (overnight)"
    if schedule.until is not None:
        span += f", until {schedule.until:%Y-%m-%d}"
    return f"nightly {span}"
