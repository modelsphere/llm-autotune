"""Agent API: what an LLM reads to write a performance report.

Read models over rows the platform already keeps (see docs/api/agent-api.md),
plus one table of saved reports. Nouns and ids only; the one write is saving a
report. Everything else the agent may want is a link away.
"""

from __future__ import annotations

import base64
import binascii
import os
from collections import deque
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import blocks as report_blocks
from app.agent import comparison as cmp
from app.agent import export as report_export
from app.agent import markdown as md
from app.agent.loader import RunBundle, load_run
from app.agent.parts import (
    benchmark_part,
    environment_part,
    launch_part,
    links_for,
    results_part,
)
from app.core.auth import Principal, get_principal
from app.db.base import get_async_session
from app.db.models import AgentReport, Campaign, Run
from app.objective import target_metric
from app.schemas.agent import (
    SCHEMA_VERSION,
    BenchmarkPart,
    CampaignDocument,
    CampaignRunRow,
    CampaignSummary,
    ComparisonDocument,
    EnvironmentPart,
    LaunchPart,
    ReportBlock,
    ReportCreate,
    ReportDetailOut,
    ReportOut,
    ReportTranslation,
    ResultsPart,
    RunDocument,
)

router = APIRouter(prefix="/agent/v1", tags=["agent"])

_MAX_ASSET_BYTES = 8 * 1024 * 1024  # all assets of one report, decoded


def _error(code: int, error: str, reasons: list[dict[str, Any]] | None = None, **extra: Any):
    return HTTPException(code, {"error": error, "reasons": reasons or [], **extra})


async def _bundle_or_404(session: AsyncSession, run_id: int) -> RunBundle:
    bundle = await load_run(session, run_id)
    if bundle is None:
        raise _error(status.HTTP_404_NOT_FOUND, "no_such_run", run_id=run_id)
    return bundle


# -- runs ------------------------------------------------------------------------


@router.get("/runs/{run_id}", response_model=RunDocument)
async def run_document(
    run_id: int,
    _: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_async_session),
):
    """Everything about one run: the five parts, unchanged, under their own keys."""
    b = await _bundle_or_404(session, run_id)
    return RunDocument(
        run_id=run_id,
        launch=launch_part(b),
        environment=environment_part(b),
        benchmark=benchmark_part(b),
        results=results_part(b),
        links=links_for(run_id),
    )


@router.get("/runs/{run_id}/launch", response_model=LaunchPart)
async def run_launch(
    run_id: int,
    _: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_async_session),
):
    return launch_part(await _bundle_or_404(session, run_id))


@router.get("/runs/{run_id}/environment", response_model=EnvironmentPart)
async def run_environment(
    run_id: int,
    _: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_async_session),
):
    return environment_part(await _bundle_or_404(session, run_id))


@router.get("/runs/{run_id}/benchmark", response_model=BenchmarkPart)
async def run_benchmark(
    run_id: int,
    _: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_async_session),
):
    return benchmark_part(await _bundle_or_404(session, run_id))


@router.get("/runs/{run_id}/results", response_model=ResultsPart)
async def run_results(
    run_id: int,
    _: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_async_session),
):
    """Measured, failed, running or queued — a failed run still answers, with
    its failure block, because failed attempts belong in a report."""
    b = await _bundle_or_404(session, run_id)
    part = results_part(b)
    if part.status in ("running", "queued"):
        raise _error(
            status.HTTP_409_CONFLICT, "run_not_finished",
            run_id=run_id, run_status=b.run.status, results_status=part.status,
            wait=True,
        )
    return part


@router.get("/runs/{run_id}/results/raw")
async def run_results_raw(
    run_id: int,
    _: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_async_session),
):
    """The LLMBench submission payload exactly as harvested."""
    b = await _bundle_or_404(session, run_id)
    if b.result is None:
        raise _error(
            status.HTTP_409_CONFLICT, "run_not_finished", run_id=run_id, run_status=b.run.status
        )
    return b.result.raw or {}


@router.get("/runs/{run_id}/log", response_class=PlainTextResponse)
async def run_log(
    run_id: int,
    tail: int = Query(default=200, ge=1, le=5000),
    _: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_async_session),
):
    run = await session.get(Run, run_id)
    if run is None:
        raise _error(status.HTTP_404_NOT_FOUND, "no_such_run", run_id=run_id)
    if not run.log_path or not os.path.exists(run.log_path):
        return "(no captured log for this run)"
    with open(run.log_path, encoding="utf-8", errors="replace") as f:
        return "".join(deque(f, maxlen=tail))


# -- campaigns ---------------------------------------------------------------


async def _campaign_or_404(session: AsyncSession, campaign_id: int) -> Campaign:
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        raise _error(status.HTTP_404_NOT_FOUND, "no_such_campaign", campaign_id=campaign_id)
    return campaign


@router.get("/campaigns", response_model=list[CampaignSummary])
async def list_campaigns(
    _: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_async_session),
):
    """Every campaign, newest first, with how many runs each one has. The
    starting point: a report is written from the runs of one campaign."""
    campaigns = (
        await session.execute(select(Campaign).order_by(Campaign.id.desc()))
    ).scalars().all()
    counts = dict(
        (
            await session.execute(
                select(Run.campaign_id, func.count(Run.id)).group_by(Run.campaign_id)
            )
        ).all()
    )
    return [
        CampaignSummary(
            id=c.id,
            name=c.name,
            status=c.status,
            model_name=c.served_model_name or "",
            engine=c.engine or "",
            benchmark_slug=c.benchmark_slug or "",
            ranking_metric=target_metric(c.objective),
            run_count=int(counts.get(c.id, 0)),
            links={"self": f"/api/agent/v1/campaigns/{c.id}"},
        )
        for c in campaigns
    ]


@router.get("/campaigns/{campaign_id}", response_model=CampaignDocument)
async def campaign_document(
    campaign_id: int,
    _: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_async_session),
):
    """One campaign's benchmark and one row per run, with the run ids a
    comparison is built from. Runs that never reached the benchmark are
    listed too, with their status — an attempt that failed is part of the
    story a report tells."""
    campaign = await _campaign_or_404(session, campaign_id)
    runs = (
        await session.execute(
            select(Run).where(Run.campaign_id == campaign.id).order_by(Run.id)
        )
    ).scalars().all()
    rows: list[CampaignRunRow] = []
    latest_measured: RunBundle | None = None
    baseline_run_id: int | None = None
    for run in runs:
        b = await load_run(session, run.id)
        if b is None:
            continue
        launch = launch_part(b)
        results = results_part(b)
        is_baseline = bool(b.candidate is not None and b.candidate.is_baseline)
        if is_baseline and baseline_run_id is None:
            baseline_run_id = run.id
        if results.status == "measured" and (
            latest_measured is None or run.id > latest_measured.run.id
        ):
            latest_measured = b
        rows.append(
            CampaignRunRow(
                run_id=run.id,
                name=f"run {run.id}",
                status=results.status,
                kind=run.kind,
                engine=launch.config.engine,
                engine_version=(run.env_snapshot or {}).get("engine_version", ""),
                image=launch.config.image,
                cards=launch.cards,
                engine_args=launch.config.engine_args,
                machine=b.machine.name if b.machine else "",
                headline=results.headline,
                passed=results.passed,
                is_baseline=is_baseline,
                finished_at=run.finished_at,
                links={"run": f"/api/agent/v1/runs/{run.id}"},
            )
        )
    if latest_measured is not None:
        benchmark = benchmark_part(latest_measured).model_copy(update={"run_id": None})
    else:
        from app.agent.parts import BenchmarkLLMBenchOut, platform_overlay

        benchmark = BenchmarkPart(
            llmbench=BenchmarkLLMBenchOut(slug=campaign.benchmark_slug or ""),
            platform=platform_overlay(campaign),
        )
    return CampaignDocument(
        id=campaign.id,
        name=campaign.name,
        status=campaign.status,
        model_name=campaign.served_model_name or "",
        model_path=campaign.model_path or "",
        engine=campaign.engine or "",
        benchmark=benchmark,
        baseline_run_id=baseline_run_id,
        runs=rows,
        links={
            "self": f"/api/agent/v1/campaigns/{campaign.id}",
            "campaign": f"/api/campaigns/{campaign.id}",
        },
    )


# -- comparison ------------------------------------------------------------------


async def _resolve(session: AsyncSession, selector: str) -> int:
    """A run id. Its own function because a comparison names runs as text in a
    query string, and a bad name should be one clear 422 rather than a parse
    error somewhere deeper."""
    token = selector.strip()
    if not token.isdigit():
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "bad_selector", selector=selector)
    return int(token)


async def _load_comparison(
    session: AsyncSession, baseline: str, attempts: str, *, force: bool, series_metrics: str
) -> ComparisonDocument:
    base_id = await _resolve(session, baseline)
    attempt_ids = [await _resolve(session, s) for s in attempts.split(",") if s.strip()]
    if not attempt_ids:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "no_attempts")
    if base_id in attempt_ids:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "baseline_is_an_attempt", run_id=base_id)
    base = await _bundle_or_404(session, base_id)
    others = [await _bundle_or_404(session, rid) for rid in attempt_ids]
    reasons = cmp.comparability(base, others)
    if reasons and not force:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "not_comparable",
            [r.model_dump() for r in reasons],
        )
    metrics = tuple(m.strip() for m in series_metrics.split(",") if m.strip())
    metrics = metrics or cmp.DEFAULT_SERIES_METRICS
    return cmp.build_comparison(base, others, reasons=reasons, series_metrics=metrics)


@router.get("/comparison", response_model=ComparisonDocument)
async def comparison(
    baseline: str = Query(..., description="run id"),
    attempts: str = Query(
        ..., description="comma-separated run ids, in ablation order"
    ),
    format: str = Query(default="json", pattern="^(json|markdown)$"),
    force: bool = Query(default=False, description="return the document even when not comparable"),
    series_metrics: str = Query(
        default="", description="per-level metrics to chart; default set when empty"
    ),
    _: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_async_session),
):
    """The report's data: one baseline, the chosen attempts, nothing else.
    Submissions not named here do not appear anywhere in the document."""
    doc = await _load_comparison(
        session, baseline, attempts, force=force, series_metrics=series_metrics
    )
    if format == "markdown":
        doc.rendered = md.render(doc)
    return doc


# -- reports ------------------------------------------------------------------


def _campaign_of(doc: ComparisonDocument) -> int | None:
    """The campaign a report is filed under: the one every run in the
    comparison came from. None when they span campaigns, which is allowed —
    the filter is a convenience, not a constraint on what may be compared."""
    ids = {doc.baseline.launch.origin.campaign_id} | {
        a.launch.origin.campaign_id for a in doc.attempts
    }
    ids.discard(None)
    return next(iter(ids)) if len(ids) == 1 else None


def _report_out(r: AgentReport) -> ReportOut:
    return ReportOut(
        id=r.id, title=r.title, baseline_run_id=r.baseline_run_id,
        attempt_run_ids=list(r.attempt_run_ids or []), campaign_id=r.campaign_id,
        comparable=r.comparable, generator=dict(r.generator or {}),
        created_by_name=r.created_by_name or "", created_at=r.created_at,
        asset_names=[a.get("name", "") for a in (r.assets or []) if isinstance(a, dict)],
        url=f"/reports/{r.id}",
        lang=r.lang or "en",
        translation_of=r.translation_of,
        labels=list(r.labels or []),
    )


async def _translations(session: AsyncSession, r: AgentReport) -> list[AgentReport]:
    """Every language of this report, itself included, first language first."""
    root = r.translation_of or r.id
    rows = (await session.execute(
        select(AgentReport)
        .where((AgentReport.id == root) | (AgentReport.translation_of == root))
        .order_by(AgentReport.id)
    )).scalars().all()
    return list(rows) or [r]


def _detail(r: AgentReport, translations: list[AgentReport]) -> ReportDetailOut:
    return ReportDetailOut(
        **_report_out(r).model_dump(),
        markdown=r.markdown,
        comparison=r.comparison or {},
        translations=[
            ReportTranslation(id=x.id, lang=x.lang or "en", title=x.title) for x in translations
        ],
    )


@router.get("/reports", response_model=list[ReportOut])
async def list_reports(
    campaign_id: int | None = Query(default=None),
    _: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_async_session),
):
    stmt = select(AgentReport).order_by(AgentReport.id.desc())
    if campaign_id is not None:
        stmt = stmt.where(AgentReport.campaign_id == campaign_id)
    return [_report_out(r) for r in (await session.execute(stmt)).scalars().all()]


@router.post("/reports", response_model=ReportDetailOut, status_code=status.HTTP_201_CREATED)
async def create_report(
    body: ReportCreate,
    dry_run: bool = Query(
        default=False, description="check the report (blocks, labels, translation) and save nothing"
    ),
    principal: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_async_session),
):
    """Save a report with the comparison it was written from, frozen.

    Its chart/table/command blocks must all resolve against that comparison
    (`GET /report-blocks` lists them); `dry_run=true` runs every check and
    saves nothing."""
    total = 0
    assets: list[dict[str, Any]] = []
    names: set[str] = set()
    for asset in body.assets:
        try:
            decoded = base64.b64decode(asset.data_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise _error(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "bad_asset", name=asset.name
            ) from exc
        total += len(decoded)
        if total > _MAX_ASSET_BYTES:
            raise _error(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "assets_too_large", limit=_MAX_ASSET_BYTES
            )
        if asset.name in names:
            raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "duplicate_asset", name=asset.name)
        names.add(asset.name)
        assets.append({
            "name": asset.name, "content_type": asset.content_type,
            "data_base64": asset.data_base64,
        })
    doc = await _load_comparison(
        session,
        str(body.baseline_run_id),
        ",".join(str(i) for i in body.attempt_run_ids),
        force=True,
        series_metrics="",
    )
    if not doc.comparable and body.comparable:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "not_comparable",
            [r.model_dump() for r in doc.reasons],
            detail=(
                "save with comparable=false to record a report over runs the platform "
                "would not compare"
            ),
        )
    bad_blocks = report_blocks.problems(body.markdown, doc)
    if bad_blocks:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "bad_blocks", bad_blocks,
            detail="GET /api/agent/v1/report-blocks lists the blocks this comparison can draw",
        )
    if body.labels and len(body.labels) != 1 + len(body.attempt_run_ids):
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "bad_labels",
            detail=f"labels needs {1 + len(body.attempt_run_ids)} names, baseline first",
        )
    translation_of = None
    if body.translation_of is not None:
        original = await session.get(AgentReport, body.translation_of)
        if original is None:
            raise _error(status.HTTP_404_NOT_FOUND, "no_such_report",
                         report_id=body.translation_of)
        if (original.baseline_run_id, list(original.attempt_run_ids or [])) != (
                body.baseline_run_id, list(body.attempt_run_ids)):
            raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, "translation_runs_differ",
                         detail="a translation is written from the same runs as its original")
        translation_of = original.translation_of or original.id
        taken = {x.lang for x in await _translations(session, original)}
        if body.lang in taken:
            raise _error(status.HTTP_409_CONFLICT, "language_exists", lang=body.lang,
                         report_id=translation_of)
    if dry_run:
        return JSONResponse({"ok": True, "blocks": len(report_blocks.parse(body.markdown)),
                             "comparable": doc.comparable})
    report = AgentReport(
        title=body.title,
        lang=body.lang,
        translation_of=translation_of,
        labels=list(body.labels),
        baseline_run_id=body.baseline_run_id,
        attempt_run_ids=list(body.attempt_run_ids),
        campaign_id=_campaign_of(doc),
        comparable=doc.comparable,
        markdown=body.markdown,
        comparison=doc.model_dump(mode="json"),
        assets=assets,
        generator=dict(body.generator or {}),
        created_by=principal.user.id,
        created_by_name=principal.actor,
    )
    session.add(report)
    await session.commit()
    await session.refresh(report)
    return _detail(report, await _translations(session, report))


@router.get("/reports/{report_id}", response_model=ReportDetailOut)
async def get_report(
    report_id: int,
    _: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_async_session),
):
    report = await session.get(AgentReport, report_id)
    if report is None:
        raise _error(status.HTTP_404_NOT_FOUND, "no_such_report", report_id=report_id)
    return _detail(report, await _translations(session, report))


@router.get("/reports/{report_id}/export.html", response_class=HTMLResponse)
async def export_report(
    report_id: int,
    download: bool = Query(default=False, description="serve as an attachment"),
    _: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_async_session),
):
    """The report as ONE self-contained HTML file: every language it exists
    in (with a switch), its frozen comparison, its assets inlined, and the
    renderer itself — no request leaves the page. Publishable as-is."""
    report = await session.get(AgentReport, report_id)
    if report is None:
        raise _error(status.HTTP_404_NOT_FOUND, "no_such_report", report_id=report_id)
    try:
        html = report_export.render_html(report, await _translations(session, report))
    except report_export.RendererMissing as exc:
        raise _error(status.HTTP_503_SERVICE_UNAVAILABLE, "renderer_not_built",
                     detail=str(exc)) from exc
    headers = {}
    if download:
        headers["Content-Disposition"] = (
            f'attachment; filename="{report_export.filename(report)}"'
        )
    return HTMLResponse(html, headers=headers)


@router.get("/reports/{report_id}/bundle.zip")
async def download_report_bundle(
    report_id: int,
    _: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_async_session),
):
    """The report's source as a zip — every language's markdown, the frozen
    comparison its blocks are drawn from, its assets, a manifest and a README —
    plus the renderer and the rendered page, so anyone can render it again."""
    report = await session.get(AgentReport, report_id)
    if report is None:
        raise _error(status.HTTP_404_NOT_FOUND, "no_such_report", report_id=report_id)
    data = report_export.render_zip(report, await _translations(session, report))
    return Response(data, media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{report_export.bundle_name(report)}.zip"',
    })


@router.get("/report-blocks", response_model=list[ReportBlock])
async def list_report_blocks(
    baseline: str = Query(..., description="run id"),
    attempts: str = Query(..., description="comma-separated, in ablation order"),
    force: bool = Query(default=False),
    _: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_async_session),
):
    """Every chart/table/command block a report over these runs can carry,
    each with the markdown to paste."""
    doc = await _load_comparison(session, baseline, attempts, force=force, series_metrics="")
    return report_blocks.available(doc)


@router.get("/reports/{report_id}/assets/{name}")
async def get_report_asset(
    report_id: int,
    name: str,
    _: Principal = Depends(get_principal),
    session: AsyncSession = Depends(get_async_session),
):
    report = await session.get(AgentReport, report_id)
    if report is None:
        raise _error(status.HTTP_404_NOT_FOUND, "no_such_report", report_id=report_id)
    asset = next(
        (a for a in (report.assets or []) if isinstance(a, dict) and a.get("name") == name), None
    )
    if asset is None:
        raise _error(status.HTTP_404_NOT_FOUND, "no_such_asset", name=name)
    return Response(
        content=base64.b64decode(asset.get("data_base64", "")),
        media_type=asset.get("content_type") or "application/octet-stream",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.get("/")
async def index(_: Principal = Depends(get_principal)):
    """Entry point: where everything else is."""
    return {
        "kind": "agent_api",
        "schema_version": SCHEMA_VERSION,
        "docs": "/api/docs",
        "contract": "docs/api/agent-api.md",
        "links": {
            "campaigns": "/api/agent/v1/campaigns",
            "campaign": "/api/agent/v1/campaigns/{campaign_id}",
            "run": "/api/agent/v1/runs/{run_id}",
            "comparison": "/api/agent/v1/comparison?baseline={run}&attempts={run},{run}",
            "comparison_markdown": (
                "/api/agent/v1/comparison?baseline={run}&attempts={run}&format=markdown"
            ),
            "reports": "/api/agent/v1/reports",
            "report_blocks": "/api/agent/v1/report-blocks?baseline={run}&attempts={run}",
            "report_export": "/api/agent/v1/reports/{id}/export.html",
            "report_bundle": "/api/agent/v1/reports/{id}/bundle.zip",
        },
    }
