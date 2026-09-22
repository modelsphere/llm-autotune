"""Promotion endpoints — turn a winning configuration into a production change.

A campaign winner (the leaderboard's top, or a named run) resolves to a
LaunchConfig plus the evidence for it, finds the baseline production runs that
config against, and — when that baseline is bound to a deploy target — becomes
a change draft against the file that target deploys from: a knob-level diff
under the baseline's ownership policy.

`…/promote/preview` builds the draft and returns it without writing anything:
the change table, what is deliberately not applied, whether the platform's
copy of the file is stale, the exact diff. `…/promote` records a Promotion
and hands the draft to the configured PromotionTarget (manual by default,
gitlab when wired — dry-run until armed). The rest read and refresh records.
"""

from __future__ import annotations

from typing import Any

import anyio
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.campaigns import leaderboard
from app.control.gitlab_client import GitLabError, GitLabUnavailable
from app.control.launch_config import LaunchConfig
from app.control.promotion import (
    TERMINAL_PROMOTION_STATES,
    PromotionError,
    PromotionHandle,
    PromotionRequest,
    PromotionState,
    build_promotion_config,
    get_target,
)
from app.control.promotion.merge_request import MergeRequestDraft, Origin, build_merge_request
from app.control.promotion.winner import baseline_lookup_order, evidence_of
from app.core.auth import get_current_user
from app.core.config import get_settings
from app.db.base import get_async_session
from app.db.models import (
    Baseline,
    Campaign,
    Candidate,
    CandidateKind,
    Event,
    Machine,
    Promotion,
    Result,
    Run,
    RunStatus,
    User,
)
from app.evaluation.aggregate import summary_of
from app.hardware import normalize_gpu_type
from app.objective import sort_key, target_metric
from app.schemas.core import (
    MergeRequestPreview,
    PromoteRequest,
    PromotionOut,
)

router = APIRouter(tags=["promotions"])


# -- resolving a winner ----------------------------------------------------------


async def _latest_llmbench_result(session: AsyncSession, run_id: int) -> Result | None:
    return (
        (
            await session.execute(
                select(Result)
                .where(Result.run_id == run_id, Result.source == "llmbench")
                .order_by(Result.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def _card_type_of(session: AsyncSession, run: Run) -> str:
    snap = run.env_snapshot or {}
    card = snap.get("card_type") or snap.get("machine_gpu_type") or ""
    if not card and run.machine_id:
        machine = await session.get(Machine, run.machine_id)
        card = machine.gpu_type if machine is not None else ""
    return normalize_gpu_type(card or "")


async def _resolve_baseline(
    session: AsyncSession, served_model_name: str, engine: str, card_type: str
) -> Baseline | None:
    """Exact (model, engine, card) first, then the "any card" wildcard — the
    same rule the supervisor applies, with the binding loaded."""
    for card in baseline_lookup_order(card_type):
        found = (
            (
                await session.execute(
                    select(Baseline)
                    .options(selectinload(Baseline.binding))
                    .where(
                        Baseline.served_model_name == served_model_name,
                        Baseline.engine == engine,
                        Baseline.card_type == card,
                    )
                )
            )
            .scalars()
            .first()
        )
        if found is not None:
            return found
    return None


async def _pick_winner(campaign_id: int, user: User, session: AsyncSession):
    """The current top of the leaderboard — the same ranking the board shows.
    Skips the production baseline row: it is the reference, not a candidate."""
    board = await leaderboard(campaign_id, user, session)
    for entry in board:
        if entry.is_baseline:
            continue
        return entry
    return None


async def _best_verify_result(
    session: AsyncSession, campaign_id: int, verify_obj: dict
) -> Result | None:
    """The winning entrant's best full-replay result — verification stage
    only, feasible first."""
    metric = target_metric(verify_obj)
    results = (
        (
            await session.execute(
                select(Result)
                .join(Run, Result.run_id == Run.id)
                .join(Candidate, Run.candidate_id == Candidate.id)
                .where(
                    Run.campaign_id == campaign_id,
                    Run.status == RunStatus.SUCCEEDED.value,
                    Candidate.kind == CandidateKind.VERIFICATION.value,
                    Result.source == "llmbench",
                )
            )
        )
        .scalars()
        .all()
    )
    best_key, best = None, None
    for r in results:
        if metric not in (r.metrics or {}):
            continue
        summary = summary_of(r, verify_obj)
        if summary.objective_value is None:
            continue
        key = (not summary.feasible, *sort_key(summary.objective_value, verify_obj))
        if best_key is None or key < best_key:
            best_key, best = key, r
    return best


# The evidence block and the baseline rule are shared with the unattended path
# (app/control/promotion/winner.py): a proposal must read the same whether a
# person opened it or the supervisor did.
_evidence = evidence_of


class _Resolved:
    """A winner, resolved: everything the draft and the Promotion row need."""

    def __init__(
        self,
        campaign: Campaign,
        run: Run,
        config: dict[str, Any],
        target: LaunchConfig,
        baseline: Baseline | None,
        origin: Origin,
        evidence: dict[str, Any],
        holds_redlines: bool = True,
        deploy_branch: str = "",
    ):
        self.campaign, self.run, self.config, self.target = campaign, run, config, target
        self.baseline, self.origin, self.evidence = baseline, origin, evidence
        self.holds_redlines = holds_redlines
        # The release branch this winner belongs on, when the campaign named
        # one. Empty leaves the choice to the baseline's binding.
        self.deploy_branch = deploy_branch


async def _resolve_campaign(
    campaign_id: int, run_id: int | None, user: User, session: AsyncSession
) -> _Resolved:
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such campaign")
    entry = None
    if run_id is not None:
        run = await session.get(Run, run_id)
        if run is None or run.campaign_id != campaign_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such run in this campaign")
        board = await leaderboard(campaign_id, user, session)
        entry = next((e for e in board if e.run_id == run_id), None)
        holds = entry.holds_redlines if entry is not None else True
    else:
        entry = await _pick_winner(campaign_id, user, session)
        if entry is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "no successful, benchmarked run to promote yet"
            )
        run = await session.get(Run, entry.run_id)
        holds = entry.holds_redlines
    if run.status != "succeeded":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"run {run.id} did not succeed (status: {run.status}); nothing to promote",
        )
    candidate = await session.get(Candidate, run.candidate_id)
    result = await _latest_llmbench_result(session, run.id)
    config = build_promotion_config(campaign, candidate, run, result)
    card = await _card_type_of(session, run)
    target = LaunchConfig.from_promotion_config(config, gpu_type=card)
    baseline = await _resolve_baseline(session, campaign.served_model_name, campaign.engine, card)
    stage = entry.stage if entry is not None else ""
    evidence = _evidence(
        config,
        vs_baseline=entry.vs_baseline if entry is not None else None,
        stage=stage or None,
        benchmark_slug=(
            campaign.verify_benchmark_slug if stage == "verify" else campaign.benchmark_slug
        )
        or None,
        target_metric=entry.target_metric if entry is not None and entry.target_metric else None,
        score=entry.score if entry is not None else None,
    )
    origin = Origin(
        kind="campaign", campaign_id=campaign.id, campaign_name=campaign.name, run_id=run.id
    )
    deploy_branch = campaign.deploy_branch
    return _Resolved(
        campaign, run, config, target, baseline, origin, evidence, holds, deploy_branch
    )


def _branch_of(resolved: _Resolved, asked: str) -> str:
    """Which release branch the merge request goes onto: what this request
    names, else what the campaign was configured with,
    else — inside the builder — the branch the bound baseline tracks."""
    return (asked or resolved.deploy_branch or "").strip()


async def _draft(
    resolved: _Resolved,
    *,
    actor: str,
    notes: str,
    apply_removals: bool,
    promote_fields: list[str],
    branch: str = "",
) -> MergeRequestDraft:
    settings = get_settings()
    try:
        return await anyio.to_thread.run_sync(
            lambda: build_merge_request(
                resolved.target,
                resolved.baseline,
                resolved.origin,
                branch=_branch_of(resolved, branch),
                apply_removals=apply_removals,
                promote_fields=frozenset(promote_fields),
                actor=actor,
                notes=notes,
                evidence=resolved.evidence,
                ui_url=settings.public_ui_url,
            )
        )
    except GitLabUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except GitLabError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


def _preview(resolved: _Resolved, draft: MergeRequestDraft) -> MergeRequestPreview:
    settings = get_settings()
    return MergeRequestPreview(
        campaign_id=resolved.campaign.id,
        run_id=resolved.run.id,
        dry_run=settings.promotion_dry_run,
        **{k: v for k, v in draft.payload().items() if k != "new_text"},
    )


async def _promote(
    resolved: _Resolved,
    *,
    user: User,
    session: AsyncSession,
    target_name: str,
    notes: str,
    force: bool,
    apply_removals: bool,
    promote_fields: list[str],
    branch: str = "",
) -> Promotion:
    if not resolved.holds_redlines and not force:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"run {resolved.run.id} crosses a redline; pass force=true to promote it anyway",
        )
    settings = get_settings()
    resolved_target = target_name or settings.promotion_target or "manual"
    draft: MergeRequestDraft | None = None
    if resolved_target == "gitlab":
        draft = await _draft(
            resolved,
            actor=user.username,
            notes=notes,
            apply_removals=apply_removals,
            promote_fields=promote_fields,
            branch=branch,
        )
        if not draft.ready:
            raise HTTPException(status.HTTP_409_CONFLICT, draft.reason)
        # A branch named on the request sticks to the campaign: the next winner
        # of the same tuning goes to the same release branch without anyone
        # having to remember which one it was. Not for a Hub measurement — its
        # campaign is a hidden one-config row nobody reads.
        if branch:
            resolved.campaign.deploy_branch = branch.strip()

    promotion = Promotion(
        campaign_id=resolved.campaign.id,
        run_id=resolved.run.id,
        target=resolved_target,
        state=PromotionState.DRAFT.value,
        config={
            **resolved.config,
            "origin": resolved.origin.__dict__,
            "evidence": resolved.evidence,
        },
        created_by=user.id,
    )
    session.add(promotion)
    await session.flush()

    request = PromotionRequest(
        config=resolved.config,
        campaign_id=resolved.campaign.id,
        campaign_name=resolved.campaign.name,
        run_id=resolved.run.id,
        actor=user.username,
        notes=notes,
        draft=draft.payload() if draft is not None else None,
    )
    try:
        target = get_target(target_name)
        handle = await anyio.to_thread.run_sync(target.open_rollout, request)
        promotion.target = target.name
        promotion.state = handle.state.value
        promotion.refs = handle.refs
        promotion.detail = handle.detail
    except (PromotionError, ValueError) as exc:
        promotion.state = PromotionState.FAILED.value
        promotion.error = str(exc)[:2000]

    session.add(
        Event(
            actor=user.username,
            kind="promotion_opened",
            campaign_id=resolved.campaign.id,
            run_id=resolved.run.id,
            payload={
                "promotion": promotion.id,
                "target": promotion.target,
                "state": promotion.state,
                "origin": resolved.origin.kind,
                "mr_url": (promotion.refs or {}).get("mr_url", ""),
            },
        )
    )
    await session.commit()
    await session.refresh(promotion)
    return promotion


# -- campaign -------------------------------------------------------------------


@router.post("/campaigns/{campaign_id}/promote/preview", response_model=MergeRequestPreview)
async def preview_campaign_promotion(
    campaign_id: int,
    body: PromoteRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    resolved = await _resolve_campaign(campaign_id, body.run_id, user, session)
    draft = await _draft(
        resolved,
        actor=user.username,
        notes=body.notes,
        apply_removals=body.apply_removals,
        promote_fields=body.promote_fields,
        branch=body.branch,
    )
    return _preview(resolved, draft)


@router.post("/campaigns/{campaign_id}/promote", response_model=PromotionOut)
async def promote_winner(
    campaign_id: int,
    body: PromoteRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    resolved = await _resolve_campaign(campaign_id, body.run_id, user, session)
    if body.run_id is not None:
        resolved.holds_redlines = True  # an explicit pick: trust the operator
    return await _promote(
        resolved,
        user=user,
        session=session,
        target_name=body.target,
        notes=body.notes,
        force=body.force,
        apply_removals=body.apply_removals,
        promote_fields=body.promote_fields,
        branch=body.branch,
    )


# -- promotions ----------------------------------------------------------------


@router.get("/promotions", response_model=list[PromotionOut])
async def list_promotions(
    campaign_id: int | None = None,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    query = select(Promotion).order_by(Promotion.id.desc())
    if campaign_id is not None:
        query = query.where(Promotion.campaign_id == campaign_id)
    return (await session.execute(query)).scalars().all()


@router.get("/promotions/{promotion_id}", response_model=PromotionOut)
async def get_promotion(
    promotion_id: int,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    promotion = await session.get(Promotion, promotion_id)
    if promotion is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such promotion")
    return promotion


@router.post("/promotions/{promotion_id}/refresh", response_model=PromotionOut)
async def refresh_promotion(
    promotion_id: int,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """Re-poll the external system for this rollout's state. A no-op once the
    promotion is terminal — a merged MR does not un-merge."""
    promotion = await session.get(Promotion, promotion_id)
    if promotion is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such promotion")
    if promotion.state in {s.value for s in TERMINAL_PROMOTION_STATES}:
        return promotion

    handle = PromotionHandle(
        target=promotion.target,
        state=PromotionState(promotion.state),
        refs=promotion.refs or {},
        detail=promotion.detail,
    )
    try:
        target = get_target(promotion.target)
        result = await anyio.to_thread.run_sync(target.status, handle)
        promotion.state = result.state.value
        promotion.detail = result.detail
        if result.refs:
            promotion.refs = {**(promotion.refs or {}), **result.refs}
    except (PromotionError, ValueError) as exc:
        promotion.error = str(exc)[:2000]
    await session.commit()
    await session.refresh(promotion)
    return promotion


@router.post("/promotions/{promotion_id}/cancel", response_model=PromotionOut)
async def cancel_promotion(
    promotion_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    promotion = await session.get(Promotion, promotion_id)
    if promotion is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such promotion")
    if promotion.state in {s.value for s in TERMINAL_PROMOTION_STATES}:
        raise HTTPException(status.HTTP_409_CONFLICT, f"promotion is already {promotion.state}")
    handle = PromotionHandle(
        target=promotion.target,
        state=PromotionState(promotion.state),
        refs=promotion.refs or {},
        detail=promotion.detail,
    )
    try:
        target = get_target(promotion.target)
        await anyio.to_thread.run_sync(target.cancel, handle)
    except (PromotionError, ValueError) as exc:
        promotion.error = str(exc)[:2000]
    promotion.state = PromotionState.CANCELLED.value
    session.add(
        Event(
            actor=user.username,
            kind="promotion_cancelled",
            campaign_id=promotion.campaign_id,
            run_id=promotion.run_id,
            payload={"promotion": promotion.id},
        )
    )
    await session.commit()
    await session.refresh(promotion)
    return promotion


def campaign_default_target() -> str:
    return get_settings().promotion_target or "manual"
