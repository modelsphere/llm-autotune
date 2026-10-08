"""What a campaign has learned so far, in a search algorithm's vocabulary.

A planner plugin proposes the next configurations from this (app/plugins.py,
`propose_candidates`). The objective is applied here, not by the planner:
every search algorithm must see the same number the report and the
leaderboard rank on, and none of them should have to know how a redline is
spelled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.control.launch.failures import is_config_induced
from app.control.orchestrator.lifecycle import as_utc
from app.db.models import (
    TERMINAL_RUN_STATES,
    Campaign,
    Candidate,
    CandidateStatus,
    Result,
    Run,
    RunKind,
    RunStatus,
)
from app.evaluation.aggregate import summary_of
from app.staging import VERIFY, stage_of


@dataclass
class RunRecord:
    """What a planner sees about a past or ongoing run. Deliberately narrow —
    no run id, no machine, no logs: a planner that could reach those would
    stop being swappable.

    The fields below `metrics` are the objective already applied, resolved
    when the result landed. A planner that had to reduce ~40 raw metric keys
    itself would need to know which one the campaign is optimizing and how its
    redlines are spelled — that is the platform's job, not the search
    algorithm's, and doing it here is what lets a grid and a Bayesian
    optimizer read the identical history.
    """

    config: dict[str, Any]
    status: str
    failure_class: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)

    # The campaign's target metric for this run. Direction is NOT applied —
    # the objective tells a planner whether higher or lower is better, and
    # pre-negating here would disagree with the number in the report.
    objective_value: float | None = None
    # Ran, produced the target metric, and held every redline.
    feasible: bool = False
    # Signed redline slacks, satisfied when <= 0. Empty when the campaign
    # declares no redlines, which means "unconstrained", not "all crossed".
    constraints: tuple[float, ...] = ()
    # The CONFIG failed (oom, bad_config, benchmark_not_passed) rather than
    # the infrastructure. An infeasible observation worth learning from —
    # unlike an ssh timeout, which says nothing about the parameters and is
    # retried on a fresh machine.
    config_induced_failure: bool = False
    # Still being measured — a live run, or a queued candidate a run will
    # start for. Evidence on the way rather than free space: a model-based
    # planner should sample near such a point, not on it.
    in_flight: bool = False
    duration_seconds: float | None = None


def elapsed_seconds(run: Run) -> float | None:
    """Wall-clock a finished run took, launch to verdict.

    Cost, in the only currency a night has. A planner that knows a config
    takes 90 minutes can weigh it against one that takes 20 — and an
    unfinished run has no duration rather than a duration of zero.
    """
    started, finished = as_utc(run.started_at), as_utc(run.finished_at)
    if started is None or finished is None:
        return None
    return max(0.0, (finished - started).total_seconds())


def campaign_history(session: Session, campaign: Campaign) -> list[RunRecord]:
    """Every run this campaign has made, and every candidate still queued or
    rejected, as RunRecords."""
    records: list[RunRecord] = []
    terminal = {s.value for s in TERMINAL_RUN_STATES}
    runs = session.scalars(select(Run).where(Run.campaign_id == campaign.id)).all()
    for run in runs:
        # The baseline canary measures production, not a point of this search
        # space; feeding its config back would teach a planner that
        # `{"__baseline__": ...}` is worth revisiting.
        if run.kind == RunKind.BASELINE.value:
            continue
        # A verification run re-measures a point the screening runs already
        # represent, against a benchmark whose metrics are named differently:
        # scored by the screening objective it would read as a config that
        # produced nothing.
        if stage_of(run.candidate) == VERIFY:
            continue
        result = session.scalars(
            select(Result)
            .where(Result.run_id == run.id, Result.source == "llmbench")
            .order_by(Result.id.desc())
            .limit(1)
        ).first()
        summary = summary_of(result, campaign.objective)
        records.append(
            RunRecord(
                config=run.candidate.config,
                status=run.status,
                failure_class=run.failure_class,
                metrics=(result.metrics if result else {}) or {},
                objective_value=summary.objective_value,
                feasible=summary.feasible and run.status == RunStatus.SUCCEEDED.value,
                constraints=summary.constraints,
                config_induced_failure=is_config_induced(run.failure_class),
                in_flight=run.status not in terminal,
                duration_seconds=elapsed_seconds(run),
            )
        )
    # A candidate already in the system — queued, or rejected before it ran —
    # counts as tried too, so a planner does not propose it again.
    ran = set(
        session.scalars(
            select(Candidate.config_hash)
            .join(Run, Run.candidate_id == Candidate.id)
            .where(Candidate.campaign_id == campaign.id)
        ).all()
    )
    for candidate in session.scalars(
        select(Candidate).where(Candidate.campaign_id == campaign.id)
    ).all():
        if candidate.config_hash in ran:
            continue
        records.append(
            RunRecord(
                config=candidate.config,
                status=f"candidate_{candidate.status}",
                # A statically rejected config is the config's fault, exactly
                # like an OOM: an infeasible point learned without a launch.
                config_induced_failure=candidate.status == CandidateStatus.INVALID.value,
                # Queued means committed: a run will measure it.
                in_flight=candidate.status
                in (CandidateStatus.PENDING.value, CandidateStatus.VALID.value),
            )
        )
    return records
