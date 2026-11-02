"""Quality floors relative to production.

A config that changes the model's numerics (a precision, a kernel) can be
faster and worse. A campaign that checks quality measures production and its
finalists with the same quality suite (app/evaluation/benchmark_spec.py,
QualitySpec) and holds each finalist to a floor relative to production's
score: a redline `{"metric": "opencompass/accuracy[gpqa_diamond]", "op": ">=",
"relative_to": "production", "value": -0.10}` reads "no more than ten points
below production".

Production's score is the average of every measurement of production in the
campaign that reports the metric, so a second measurement of production
narrows the noise instead of replacing the first. When one lands, every
result held to that floor is judged again (`rescore`): a finalist measured
before production was had nothing to be compared with, and crossed.
"""

from __future__ import annotations

import copy
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Campaign, Candidate, PolicyTrial, Result, Run, is_baseline_candidate
from app.evaluation.aggregate import ResultSummary, summarize
from app.evaluation.benchmark_spec import QUALITY_MODULE
from app.objective import redlines

PRODUCTION = "production"


def relative_redlines(objective: dict | None) -> list[dict]:
    return [r for r in redlines(objective) if r.get("relative_to") == PRODUCTION]


def quality_floor(tasks: list[str], max_drop: float) -> list[dict]:
    """The redlines that hold each task within `max_drop` (a fraction: 0.10
    is ten points) of production."""
    # Named with its scenario: an unnamed reference reads the benchmark's
    # main (throughput) scenario, which has no accuracy.
    return [
        {"metric": f"{QUALITY_MODULE}/accuracy[{task}]", "op": ">=",
         "relative_to": PRODUCTION, "value": -abs(max_drop)}
        for task in tasks
    ]


def is_production(candidate: Candidate | None, campaign: Campaign) -> bool:
    """A measurement of production: the platform's reference (validation's
    production run, a baseline), or, in a recipe campaign, a config that
    leaves the recipe as it is: none at all, or one that only removes flags
    the recipe never set (a search space lists configs side by side with
    `null` for a flag one of them lacks)."""
    if candidate is None:
        return False
    if is_baseline_candidate(candidate):
        return True
    if not getattr(campaign, "recipe", None):
        return False
    config = candidate.config or {}
    if not config:
        return True
    from app.control.recipe.campaign import base_recipe, recipe_for

    try:
        return recipe_for(campaign, config).model_dump() == base_recipe(campaign).model_dump()
    except Exception:
        return False


def production_scores(db: Session, campaign: Campaign, metrics: list[str]) -> dict[str, float]:
    """Production's score on each metric: the mean of its measurements."""
    from app.measures import reader_for

    if not metrics:
        return {}
    rows = db.execute(
        select(Result, Candidate)
        .join(Run, Result.run_id == Run.id)
        .join(Candidate, Run.candidate_id == Candidate.id)
        .where(Run.campaign_id == campaign.id, Result.source == "llmbench")
    ).all()
    seen: dict[str, list[float]] = {m: [] for m in metrics}
    for result, candidate in rows:
        if not is_production(candidate, campaign):
            continue
        try:
            reader = reader_for(result.metrics, result.raw, candidate.config, campaign)
        except Exception:
            continue
        for metric in metrics:
            value = reader.get(metric)
            if isinstance(value, int | float) and not isinstance(value, bool):
                seen[metric].append(float(value))
    return {m: sum(v) / len(v) for m, v in seen.items() if v}


def resolve(objective: dict | None, scores: dict[str, float]) -> dict | None:
    """The objective with each relative redline made absolute against
    production's score; one with no score yet is left relative (and crossed
    when judged)."""
    if not relative_redlines(objective):
        return objective
    resolved = copy.deepcopy(objective)
    key = "redlines" if "redlines" in resolved else "constraints"
    out = []
    for redline in resolved.get(key) or []:
        metric = redline.get("metric")
        if redline.get("relative_to") == PRODUCTION and metric in scores:
            base, offset = scores[metric], float(redline.get("value") or 0.0)
            sign = "-" if offset < 0 else "+"
            redline = {k: v for k, v in redline.items() if k != "relative_to"}
            redline["value"] = round(base + offset, 6)
            redline["basis"] = f"production {base:.3g} {sign} {abs(offset):g}"
        out.append(redline)
    resolved[key] = out
    return resolved


def judge(db: Session, campaign: Campaign, objective: dict | None, reader: Any) -> ResultSummary:
    """summarize(), with relative redlines resolved against production."""
    needed = [r["metric"] for r in relative_redlines(objective) if r.get("metric")]
    scores = production_scores(db, campaign, needed) if needed else {}
    return summarize(resolve(objective, scores), reader)


def rescore(db: Session, campaign: Campaign, objective_of) -> int:
    """Judge again every result of the campaign that a relative redline holds,
    after production's score moved. `objective_of(run)` is the objective the
    run's stage is judged by. Returns how many results changed."""
    from app.measures import reader_for

    rows = db.execute(
        select(Result, Run, Candidate)
        .join(Run, Result.run_id == Run.id)
        .join(Candidate, Run.candidate_id == Candidate.id)
        .where(Run.campaign_id == campaign.id, Result.source == "llmbench")
    ).all()
    changed = 0
    for result, run, candidate in rows:
        objective = objective_of(run)
        if not relative_redlines(objective):
            continue
        reader = reader_for(result.metrics, result.raw, candidate.config, campaign)
        stored = judge(db, campaign, objective, reader).as_dict()
        if (stored["feasible"], stored["breaches"]) == (bool(result.feasible),
                                                       list(result.breaches or [])):
            continue
        result.objective_value = stored["objective_value"]
        result.feasible = stored["feasible"]
        result.constraints = stored["constraints"]
        result.breaches = stored["breaches"]
        trial = db.scalars(
            select(PolicyTrial).where(PolicyTrial.idempotency_key == f"platform-run-{run.id}")
        ).first()
        if trial is not None:
            trial.reported_objective_value = stored["objective_value"]
            trial.reported_feasible = stored["feasible"]
        changed += 1
    return changed
