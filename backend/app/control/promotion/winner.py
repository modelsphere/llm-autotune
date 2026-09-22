"""A campaign's winner, resolved without an event loop — and promoted by the
supervisor when the campaign asked it to.

The API resolves a winner too (`app/api/promotions.py`), from an async session
and on a person's click. This is the same decision taken by the worker when a
campaign with `auto_promote` finishes: the top of the board becomes a merge
request with nobody present. The parts that *decide* anything are shared, not
copied — the ordering lives in `app.evaluation.ranking`, the evidence block and
the baseline rule live here and are imported by the API. What differs between
the two is only how the rows are fetched, because one session is async and the
other is not.

Nothing here is a second opinion about who won.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.control.launch_config import LaunchConfig
from app.control.promotion.base import PromotionError, PromotionRequest, PromotionState
from app.control.promotion.config import build_promotion_config
from app.control.promotion.merge_request import MergeRequestDraft, Origin, build_merge_request
from app.db.models import (
    Baseline,
    Campaign,
    Candidate,
    Promotion,
    Result,
    Run,
)
from app.evaluation.ranking import card_type_of, leaderboard_entries, winner
from app.schemas.core import LeaderboardEntry

# ---------------------------------------------------------------------------
# the shared decisions
# ---------------------------------------------------------------------------


def evidence_of(config: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """What the merge request cites: the measurement, and the exact image and
    card it was taken on. Shared by every origin — a campaign winner, a
    a campaign winner — so the description of a proposal
    never depends on which button opened it."""
    image = config.get("image") or {}
    snapshot = config.get("env_snapshot") or {}
    out = {
        **(config.get("evidence") or {}),
        "image_ref": image.get("ref", ""),
        "image_tag": image.get("tag", ""),
        "image_digest": image.get("digest", ""),
        "card_type": snapshot.get("card_type") or snapshot.get("machine_gpu_type") or "",
        "engine_version": snapshot.get("engine_version", ""),
    }
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


def baseline_lookup_order(card_type: str) -> list[str]:
    """Which baselines to try, in order: the exact card first, then the "any
    card" wildcard — the same rule the supervisor applies when it decides what
    a campaign is measured against."""
    return [card_type, ""] if card_type else [""]


def origin_of(campaign: Campaign, run_id: int) -> Origin:
    """Where a config came from, for the title and the link back."""
    return Origin(
        kind="campaign", campaign_id=campaign.id, campaign_name=campaign.name, run_id=run_id
    )


def deploy_branch_of(campaign: Campaign) -> str:
    """The release branch this campaign's winner goes onto, or empty — which
    leaves the choice to the bound baseline's binding."""
    return (campaign.deploy_branch or "").strip()


# ---------------------------------------------------------------------------
# resolving, synchronously
# ---------------------------------------------------------------------------


@dataclass
class Winner:
    campaign: Campaign
    run: Run
    entry: LeaderboardEntry
    config: dict[str, Any]
    target: LaunchConfig
    baseline: Baseline | None
    origin: Origin
    evidence: dict[str, Any] = field(default_factory=dict)
    deploy_branch: str = ""
    holds_redlines: bool = True


def resolve(session: Session, campaign: Campaign) -> Winner | None:
    """The campaign's current winner, or None when it has none: no successful
    benchmarked run, or only the production baseline ran."""
    rows = session.execute(
        select(Run, Candidate, Result)
        .join(Candidate, Run.candidate_id == Candidate.id)
        .join(Result, Result.run_id == Run.id)
        .where(
            Run.campaign_id == campaign.id,
            Run.status == "succeeded",
            Result.source == "llmbench",
        )
    ).all()
    entry = winner(leaderboard_entries(campaign, list(rows)))
    if entry is None:
        return None
    run, candidate, result = next(r for r in rows if r[0].id == entry.run_id)
    config = build_promotion_config(campaign, candidate, run, result)
    card = card_type_of(run)
    baseline = None
    for card_key in baseline_lookup_order(card):
        baseline = session.scalars(
            select(Baseline)
            .options(selectinload(Baseline.binding))
            .where(
                Baseline.served_model_name == campaign.served_model_name,
                Baseline.engine == campaign.engine,
                Baseline.card_type == card_key,
            )
        ).first()
        if baseline is not None:
            break
    return Winner(
        campaign=campaign,
        run=run,
        entry=entry,
        config=config,
        target=LaunchConfig.from_promotion_config(config, gpu_type=card),
        baseline=baseline,
        origin=origin_of(campaign, run.id),
        evidence=evidence_of(
            config,
            vs_baseline=entry.vs_baseline,
            stage=entry.stage or None,
            benchmark_slug=(
                campaign.verify_benchmark_slug
                if entry.stage == "verify"
                else campaign.benchmark_slug
            )
            or None,
            target_metric=entry.target_metric or None,
            score=entry.score,
        ),
        deploy_branch=deploy_branch_of(campaign),
        holds_redlines=entry.holds_redlines,
    )


# ---------------------------------------------------------------------------
# promoting, unattended
# ---------------------------------------------------------------------------


def draft_for(found: Winner, *, actor: str, ui_url: str = "") -> MergeRequestDraft:
    return build_merge_request(
        found.target,
        found.baseline,
        found.origin,
        branch=found.deploy_branch,
        actor=actor,
        notes="Opened automatically when the campaign finished.",
        evidence=found.evidence,
        ui_url=ui_url,
    )


def failed_draft(reason: str) -> MergeRequestDraft:
    """A draft that could not be built at all — the reason travels as the
    Promotion row's error rather than as a traceback in the worker log."""
    return MergeRequestDraft(ready=False, reason=reason)


def promote(
    session: Session,
    found: Winner,
    *,
    actor: str,
    target_name: str,
    ui_url: str = "",
    draft: MergeRequestDraft | None = None,
) -> Promotion:
    """Record the promotion and hand it to the target.

    Always leaves a Promotion row, including when the draft or the target
    refuses: unattended work that fails silently is worse than unattended work
    that fails. The row is what the campaign page shows, and what stops the
    next tick from trying again.
    """
    promotion = Promotion(
        campaign_id=found.campaign.id,
        run_id=found.run.id,
        target=target_name,
        state=PromotionState.DRAFT.value,
        config={**found.config, "origin": found.origin.__dict__, "evidence": found.evidence},
    )
    session.add(promotion)
    session.flush()

    try:
        if target_name == "gitlab":
            draft = draft or draft_for(found, actor=actor, ui_url=ui_url)
            if not draft.ready:
                promotion.state = PromotionState.FAILED.value
                promotion.error = (draft.reason or "nothing to change")[:2000]
                return promotion
        from app.control.promotion import get_target  # package init; avoids a cycle

        target = get_target(target_name)
        handle = target.open_rollout(
            PromotionRequest(
                config=found.config,
                campaign_id=found.campaign.id,
                campaign_name=found.campaign.name,
                run_id=found.run.id,
                actor=actor,
                notes="Opened automatically when the campaign finished.",
                draft=draft.payload() if draft is not None else None,
            )
        )
        promotion.target = target.name
        promotion.state = handle.state.value
        promotion.refs = handle.refs
        promotion.detail = handle.detail
    except (PromotionError, ValueError, RuntimeError) as exc:
        promotion.state = PromotionState.FAILED.value
        promotion.error = str(exc)[:2000]
    return promotion
