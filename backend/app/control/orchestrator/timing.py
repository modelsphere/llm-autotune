"""How long this campaign's runs actually take, learned from the runs.

A campaign used to be told: minutes per run, minutes of model startup, minutes
per validation. Nobody knows those numbers before the first run, and a guess
either wastes the window (too high) or kills runs at its edge (too low). The
platform measures both on every run anyway — the engine is polled until it
answers, the benchmark until it returns — so it keeps the recent samples on
the campaign and answers from them.

Kept on the campaign row (`campaigns.run_timing`) rather than queried per
read, because the deadline math runs inside API heartbeats as well as worker
ticks, and neither should have to scan events to know a number.
"""

from datetime import datetime
from math import ceil

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Campaign, Event, Run, RunKind, RunStatus
from app.staging import SCREEN, VERIFY

# Recent samples kept per measure; older ones fall off, so a campaign that
# changes image mid-life follows the new numbers within a night.
KEEP = 20
# Headroom on top of the observed figure: an estimate that is exactly right
# half the time cuts the other half off at the window's edge.
MARGIN = 1.2

STARTUP = "startup"
_LAUNCHED = {RunKind.EXPERIMENT.value, RunKind.POLICY_LAUNCH.value}
_READY = {RunStatus.BENCHING.value, RunStatus.SERVING.value}


def _bench_key(stage: str) -> str:
    return f"bench_{stage}"


def _push(campaign: Campaign, key: str, minutes: float) -> None:
    timing = dict(campaign.run_timing or {})
    samples = [*timing.get(key, []), round(max(minutes, 0.0), 2)][-KEEP:]
    timing[key] = samples
    # Reassigned, not mutated: a JSON column only notices a new object.
    campaign.run_timing = timing


def _transitions(session: Session, run: Run) -> dict[str, datetime]:
    """When this run first entered each state, from its audit events."""
    rows = session.execute(
        select(Event.ts, Event.payload)
        .where(Event.run_id == run.id, Event.kind == "run_transition")
        .order_by(Event.id)
    ).all()
    first: dict[str, datetime] = {}
    for ts, payload in rows:
        to = (payload or {}).get("to")
        if to and to not in first and ts is not None:
            first[to] = ts
    return first


def _minutes(a: datetime, b: datetime) -> float:
    if a.tzinfo is None and b.tzinfo is not None:
        a = a.replace(tzinfo=b.tzinfo)
    if b.tzinfo is None and a.tzinfo is not None:
        b = b.replace(tzinfo=a.tzinfo)
    return (b - a).total_seconds() / 60


def record_ready(session: Session, run: Run, moment: datetime) -> None:
    """The engine answered: how long it took from launch to serveable."""
    if run.kind not in _LAUNCHED or run.campaign is None:
        return
    launched = _transitions(session, run).get(RunStatus.LAUNCHING.value) or run.started_at
    if launched is not None:
        _push(run.campaign, STARTUP, _minutes(launched, moment))


def record_finished(session: Session, run: Run, stage: str, moment: datetime) -> None:
    """A benchmark came back: how long the measurement itself took."""
    if run.campaign is None or run.kind == RunKind.POLICY_LAUNCH.value:
        return
    if run.kind == RunKind.EXTERNAL.value:
        # Starts at the health gate against an endpoint that is already up,
        # so all of it is measurement.
        began = run.started_at
    else:
        seen = _transitions(session, run)
        began = seen.get(RunStatus.BENCHING.value)
    if began is not None:
        _push(run.campaign, _bench_key(stage), _minutes(began, moment))


def _typical(samples: list[float]) -> float | None:
    """A high percentile rather than the mean: the estimate exists to keep a
    run from being cut off, and the slow runs are the ones that would be."""
    if not samples:
        return None
    ordered = sorted(samples)
    return ordered[max(0, ceil(0.9 * len(ordered)) - 1)]


def startup_minutes(campaign: Campaign) -> float | None:
    return _typical((campaign.run_timing or {}).get(STARTUP, []))


def bench_minutes(campaign: Campaign, stage: str = SCREEN) -> float | None:
    return _typical((campaign.run_timing or {}).get(_bench_key(stage), []))


def run_minutes(campaign: Campaign, stage: str = SCREEN) -> int | None:
    """One whole run — engine up, then measured — with headroom. None until a
    run of this stage has been seen, so the caller can fall back."""
    bench = bench_minutes(campaign, stage)
    if bench is None:
        return None
    return ceil((bench + (startup_minutes(campaign) or 0)) * MARGIN)


def validation_minutes(campaign: Campaign) -> tuple[int | None, int | None]:
    """(benchmark, startup) for validating one policy contender. Validation runs
    the verify suite; a campaign with no second benchmark verifies on its
    screen benchmark, so the search runs already measured it."""
    bench = bench_minutes(campaign, VERIFY)
    if bench is None and not (campaign.verify_benchmark_slug or "").strip():
        bench = bench_minutes(campaign, SCREEN)
    startup = startup_minutes(campaign)
    return (
        ceil(bench * MARGIN) if bench is not None else None,
        ceil(startup * MARGIN) if startup is not None else None,
    )


def summary(campaign: Campaign) -> dict[str, float | int | None]:
    """What the campaign page shows as learned."""
    timing = campaign.run_timing or {}
    return {
        "startup_minutes": startup_minutes(campaign),
        "bench_minutes": bench_minutes(campaign, SCREEN),
        "verify_bench_minutes": bench_minutes(campaign, VERIFY),
        "samples": len(timing.get(STARTUP, [])) + len(timing.get(_bench_key(SCREEN), [])),
    }


def window_minutes(campaign: Campaign, stage: str, configured: int, fallback: int) -> int:
    """How much window one more run of this stage needs before it starts.

    What the campaign's runs have shown, never more than the configured cap.
    Before any run of this stage is measured, the platform default, also
    capped: a 12-hour cap must not make a 9-hour window refuse its first run.
    """
    learned = run_minutes(campaign, stage)
    estimate = learned if learned is not None else fallback
    return min(estimate, configured) if configured else estimate
