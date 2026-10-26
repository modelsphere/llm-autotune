import shlex

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.control.launch import MachineInfo
from app.control.launch.base import LaunchSpec
from app.core.auth import get_current_user
from app.db.base import get_async_session
from app.db.models import (
    TERMINAL_RUN_STATES,
    Campaign,
    Candidate,
    Event,
    Result,
    Run,
    RunStatus,
    User,
    is_baseline_config,
)
from app.reporting.run_log import read_run_log
from app.schemas.core import ResultOut, RunDetailOut, RunOut

router = APIRouter(prefix="/runs", tags=["runs"])

_TERMINAL = {s.value for s in TERMINAL_RUN_STATES}


@router.get("", response_model=list[RunOut])
async def list_runs(
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
    limit: int = 200,
    status_in: list[str] | None = Query(
        None, alias="status",
        description="Run statuses to keep; `live` stands for every state a run is still in "
        "while it runs. Repeat the parameter for several.",
    ),
    campaign_id: list[int] | None = Query(None, description="Repeat for several campaigns."),
    owner: list[str] | None = Query(
        None, description="Usernames of the campaign owners. Repeat for several."
    ),
):
    query = select(Run).join(Campaign, Campaign.id == Run.campaign_id)
    if status_in:
        wanted = set(status_in)
        if "live" in wanted:
            wanted.discard("live")
            wanted |= {s.value for s in RunStatus} - _TERMINAL
        query = query.where(Run.status.in_(wanted))
    if campaign_id:
        query = query.where(Run.campaign_id.in_(campaign_id))
    if owner:
        query = query.join(User, User.id == Campaign.owner_id).where(User.username.in_(owner))
    # The candidate comes along because RunOut reports which stage the run
    # belongs to, and that is read off it. Lazy-loading it during serialization
    # raises MissingGreenlet under the async session rather than fetching.
    runs = (
        await session.execute(
            query.options(selectinload(Run.candidate)).order_by(Run.id.desc()).limit(limit)
        )
    ).scalars().all()
    ids = {run.campaign_id for run in runs}
    names = {
        row.id: (row.name, row.username or "")
        for row in (
            await session.execute(
                select(Campaign.id, Campaign.name, User.username)
                .outerjoin(User, User.id == Campaign.owner_id)
                .where(Campaign.id.in_(ids))
            )
        ).all()
    } if ids else {}
    out = []
    for run in runs:
        row = RunOut.model_validate(run)
        row.campaign_name, row.owner_name = names.get(run.campaign_id, ("", ""))
        out.append(row)
    return out


@router.get("/{run_id}", response_model=RunDetailOut)
async def get_run(
    run_id: int,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    run = await session.get(Run, run_id, options=[selectinload(Run.candidate)])
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such run")
    candidate = await session.get_one(Candidate, run.candidate_id)
    results = (
        (await session.execute(select(Result).where(Result.run_id == run_id).order_by(Result.id)))
        .scalars()
        .all()
    )
    campaign = await session.get_one(Campaign, run.campaign_id)
    return RunDetailOut(
        **RunOut.model_validate(run).model_dump(),
        config=candidate.config,
        env_snapshot=run.env_snapshot or {},
        results=[_with_named(r, campaign, candidate) for r in results],
        serve_command=_serve_command(run, candidate, campaign),
        image=campaign.image,
        model_path=campaign.model_path,
    )


def _objective_names(campaign: Campaign) -> list[str]:
    from app.objective import redlines, target_metric

    names: list[str] = []
    for objective in (campaign.objective or {}, campaign.verify_objective or {}):
        if not objective:
            continue
        names.append(target_metric(objective))
        names.extend(r.get("metric", "") for r in redlines(objective))
    return list(dict.fromkeys(n for n in names if n))


def _with_named(result: Result, campaign: Campaign, candidate: Candidate) -> ResultOut:
    """A result with the values its campaign's objectives name, read as the
    run was judged — a raw metrics dict has no `success_rate`."""
    from app.measures import reader_for

    out = ResultOut.model_validate(result)
    if result.source != "llmbench":
        return out
    try:
        reader = reader_for(result.metrics, result.raw, candidate.config)
        for name in _objective_names(campaign):
            value = reader.get(name)
            out.named[name] = float(value) if isinstance(value, (int, float)) else None
    except Exception:  # noqa: BLE001 — a display aid; the run detail never fails on it
        pass
    return out


def _serve_command(run: Run, candidate: Candidate, campaign: Campaign) -> str:
    """The engine command the run's config renders to, single-node, led by
    the env vars the config sets."""
    from app.control.engines import get_adapter
    from app.control.engines.env_keys import split_env

    if is_baseline_config(candidate.config):
        return ""
    try:
        spec = LaunchSpec(
            run_id=run.id,
            machine=MachineInfo(name="", host=""),
            engine=campaign.engine,
            image=campaign.image,
            model_path=campaign.model_path,
            served_model_name=campaign.served_model_name,
            engine_args=split_env(candidate.config or {})[0],
            port=run.service_port or campaign.service_port,
        )
        env = [f"{k}={v}" for k, v in sorted(split_env(candidate.config or {})[1].items())]
        return shlex.join([*env, *get_adapter(campaign.engine).build_command(spec)])
    except Exception:  # noqa: BLE001 — a display aid; the run detail never fails on it
        return ""


@router.post("/{run_id}/stop")
async def stop_run(
    run_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """Request a stop. The worker honors it on its next tick (teardown +
    machine release) — the API never touches resources directly."""
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such run")
    if run.status in _TERMINAL:
        raise HTTPException(status.HTTP_409_CONFLICT, f"run already {run.status}")
    session.add(
        Event(actor=user.username, kind="stop_requested", run_id=run.id,
              campaign_id=run.campaign_id, payload={})
    )
    await session.commit()
    return {"ok": True, "note": "stop requested; the worker applies it within one tick"}


@router.get("/{run_id}/log", response_class=PlainTextResponse)
async def get_run_log(
    run_id: int,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such run")
    return await read_run_log(session, run)
