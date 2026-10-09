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

from app.core.config import get_settings
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


def record_cut(session: Session, run: Run, stage: str, moment: datetime) -> None:
    """A run the window cut off still says something: whatever it got through
    took at least this long. Kept as a sample, so the next night reserves at
    least as much and a too-short estimate corrects itself instead of cutting
    every night's last run."""
    if run.campaign is None:
        return
    seen = _transitions(session, run)
    began = seen.get(RunStatus.BENCHING.value) or (
        run.started_at if run.kind == RunKind.EXTERNAL.value else None
    )
    if began is not None:
        _push(run.campaign, _bench_key(stage), _minutes(began, moment))
    elif run.kind in _LAUNCHED:
        launched = seen.get(RunStatus.LAUNCHING.value) or run.started_at
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


# -- before anything has been measured --------------------------------------------

PRIOR = "prior"
# Per concurrency level on top of its measured seconds: starting the level,
# draining it, writing its results.
_LEVEL_OVERHEAD_SECONDS = 30
# A replay's own time cap stands in for its length only when it is a real
# bound; LLMBench's default (hours) says nothing about how long one takes.
_REPLAY_CAP_TRUSTED_SECONDS = 2 * 3600


def estimate_benchmark_minutes(benchmark: dict) -> float | None:
    """How long a benchmark takes, from its own parameters, as LLMBench lists
    it. None when any module's length cannot be told from its parameters."""
    total = 0.0
    for module in benchmark.get("modules") or []:
        name = str(module.get("module_name") or "").split("#")[0]
        params = module.get("params_json") or module.get("params") or {}
        if name == "perf_guidellm_sweep" and params.get("search_mode", "grid") == "grid":
            levels = [c for c in str(params.get("concurrencies", "")).split(",") if c.strip()]
            if not levels:
                return None
            per_level = (float(params.get("max_seconds", 300)) + float(
                params.get("warmup_seconds", 30)) + _LEVEL_OVERHEAD_SECONDS)
            total += len(levels) * per_level / 60
        elif name == get_settings().llmbench_replay_module or name == "replay":
            cap = float(params.get("max_seconds", 0) or 0)
            if not 0 < cap <= _REPLAY_CAP_TRUSTED_SECONDS:
                return None
            total += cap / 60
        else:
            return None
    return total or None


def set_priors(campaign: Campaign, priors: dict[str, float | None]) -> None:
    """Keep the estimates a campaign starts from, by stage, until its runs
    replace them with measurements."""
    timing = dict(campaign.run_timing or {})
    timing[PRIOR] = {_bench_key(stage): round(m, 2) for stage, m in priors.items() if m}
    campaign.run_timing = timing


def _prior_bench(campaign: Campaign, stage: str) -> float | None:
    return ((campaign.run_timing or {}).get(PRIOR) or {}).get(_bench_key(stage))


def _startup_or_bound(campaign: Campaign) -> float:
    """Measured startup, or — before any engine has come up — the readiness
    timeout: the longest the platform would wait for one anyway."""
    learned = startup_minutes(campaign)
    return learned if learned is not None else float(get_settings().ready_timeout_minutes)


def run_minutes(campaign: Campaign, stage: str = SCREEN) -> int | None:
    """One whole run — engine up, then measured — with headroom: measured,
    else estimated from the benchmark's parameters. None when neither is
    known, so the caller can fall back."""
    bench = bench_minutes(campaign, stage)
    if bench is not None:
        return ceil((bench + (startup_minutes(campaign) or 0)) * MARGIN)
    prior = _prior_bench(campaign, stage)
    if prior is not None:
        return ceil((prior + _startup_or_bound(campaign)) * MARGIN)
    return None


def validation_minutes(campaign: Campaign) -> tuple[int | None, int | None]:
    """(benchmark, startup) for validating one policy contender. Validation runs
    the verify suite; a campaign with no second benchmark verifies on its
    screen benchmark, so the search runs already measured it."""
    single = not (campaign.verify_benchmark_slug or "").strip()
    bench = bench_minutes(campaign, VERIFY)
    if bench is None and single:
        bench = bench_minutes(campaign, SCREEN)
    if bench is None:
        bench = _prior_bench(campaign, VERIFY) or (
            _prior_bench(campaign, SCREEN) if single else None)
    startup = startup_minutes(campaign)
    if startup is None and bench is not None:
        startup = _startup_or_bound(campaign)
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
        # Estimated from the benchmark's parameters, before any measurement.
        "estimated_bench_minutes": _prior_bench(campaign, SCREEN),
    }


def window_minutes(campaign: Campaign, stage: str, configured: int, fallback: int) -> int:
    """How much window one more run of this stage needs before it starts.

    What the campaign's runs have shown, never more than the configured cap.
    Before any run of this stage is measured, an estimate from the benchmark's
    own parameters, else the platform default — also capped: a 12-hour cap
    must not make a 9-hour window refuse its first run.
    """
    learned = run_minutes(campaign, stage)
    estimate = learned if learned is not None else fallback
    return min(estimate, configured) if configured else estimate
