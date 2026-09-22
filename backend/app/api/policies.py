"""The human-facing side of policy-as-code: the registry (which images exist)
and the session views (what a night did). The policy containers themselves
talk to api/policy_sessions.py; nothing here is reachable with a session
token."""

import os
from datetime import UTC, datetime

import anyio
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.control.launch import MachineInfo, get_driver
from app.control.launch.base import DeploymentHandle
from app.control.orchestrator.policy_lifecycle import deadlines
from app.control.orchestrator.policy_session import policy_log_path
from app.control.search.coverage import space_coverage
from app.core.auth import get_current_user
from app.core.config import get_settings
from app.db.base import get_async_session
from app.db.models import (
    Campaign,
    Event,
    Machine,
    Policy,
    PolicyContender,
    PolicySession,
    PolicyTrial,
    User,
)
from app.schemas.policy import (
    ContenderOut,
    PolicyIn,
    PolicyOut,
    SessionDetailOut,
    SessionOut,
    SpaceCoverage,
    TrialOut,
)

router = APIRouter(tags=["policies"])


# -- registry -----------------------------------------------------------------


@router.get("/policies", response_model=list[PolicyOut])
async def list_policies(
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
):
    rows = (await db.execute(select(Policy).order_by(Policy.name))).scalars().all()
    return [PolicyOut.model_validate(row) for row in rows]


@router.post("/policies", response_model=PolicyOut, status_code=status.HTTP_201_CREATED)
async def create_policy(
    body: PolicyIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
):
    existing = (
        await db.execute(select(Policy).where(Policy.name == body.name))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"policy '{body.name}' already exists")
    policy = Policy(owner_id=user.id, **body.model_dump())
    db.add(policy)
    db.add(
        Event(actor=user.username, kind="policy_registered", payload={"name": body.name})
    )
    await db.commit()
    await db.refresh(policy)
    return PolicyOut.model_validate(policy)


@router.put("/policies/{policy_id}", response_model=PolicyOut)
async def update_policy(
    policy_id: int,
    body: PolicyIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
):
    policy = await db.get(Policy, policy_id)
    if policy is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such policy")
    for key, value in body.model_dump().items():
        setattr(policy, key, value)
    db.add(
        Event(actor=user.username, kind="policy_updated", payload={"name": policy.name})
    )
    await db.commit()
    await db.refresh(policy)
    return PolicyOut.model_validate(policy)


@router.delete("/policies/{policy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_policy(
    policy_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
):
    policy = await db.get(Policy, policy_id)
    if policy is None:
        return
    used_by = (
        await db.execute(select(Campaign.id).where(Campaign.policy_id == policy_id).limit(1))
    ).scalar_one_or_none()
    if used_by is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"policy '{policy.name}' is referenced by campaign {used_by}; detach it first",
        )
    await db.delete(policy)
    db.add(Event(actor=user.username, kind="policy_deleted", payload={"name": policy.name}))
    await db.commit()


# -- session views ---------------------------------------------------------------


@router.get("/campaigns/{campaign_id}/sessions", response_model=list[SessionOut])
async def campaign_sessions(
    campaign_id: int,
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
):
    rows = (
        (
            await db.execute(
                select(PolicySession)
                .where(PolicySession.campaign_id == campaign_id)
                .order_by(PolicySession.id.desc())
            )
        )
        .scalars()
        .all()
    )
    return [SessionOut.model_validate(row) for row in rows]


@router.get("/policy-sessions/{session_id}", response_model=SessionDetailOut)
async def session_detail(
    session_id: int,
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
):
    session = await db.get(PolicySession, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such session")
    campaign = await db.get(Campaign, session.campaign_id)
    machine = await db.get(Machine, session.machine_id) if session.machine_id else None
    trials = (
        (
            await db.execute(
                select(PolicyTrial)
                .where(PolicyTrial.session_id == session.id)
                .order_by(PolicyTrial.seq)
            )
        )
        .scalars()
        .all()
    )
    contenders = (
        (
            await db.execute(
                select(PolicyContender)
                .where(PolicyContender.session_id == session.id)
                .order_by(PolicyContender.id)
            )
        )
        .scalars()
        .all()
    )
    search, hard = deadlines(campaign, machine) if campaign else (None, None)
    detail = SessionDetailOut.model_validate(session)
    detail.search_deadline = search
    detail.hard_deadline = hard
    detail.trials = [TrialOut.model_validate(t) for t in trials]
    detail.contenders = [ContenderOut.model_validate(c) for c in contenders]
    return detail


@router.get("/policy-sessions/{session_id}/coverage", response_model=SpaceCoverage)
async def session_coverage(
    session_id: int,
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
):
    """What part of the declared space this session actually searched — derived
    from the platform's own trial ledger, with the policy's self-reported
    coverage echoed alongside. The data source for the coverage visualization."""
    session = await db.get(PolicySession, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such session")
    campaign = await db.get(Campaign, session.campaign_id)
    rows = (
        (
            await db.execute(
                select(PolicyTrial.config_hash, PolicyTrial.config).where(
                    PolicyTrial.session_id == session.id
                )
            )
        )
        .all()
    )
    trials = [(h, cfg or {}) for h, cfg in rows]
    return space_coverage(
        campaign.search_space or {} if campaign else {},
        trials,
        session.coverage or None,
    )


def _live_workload_log(machine: Machine, container_name: str) -> str:
    """Best-effort read-only fetch of a running policy container's log (docker
    logs merges stdout+stderr). Any ssh hiccup returns empty, and the caller
    falls back to the captured file."""
    try:
        driver = get_driver(machine.driver or "ssh_docker")
        handle = DeploymentHandle(
            driver=getattr(driver, "name", ""),
            container_name=container_name,
            machine=MachineInfo.of(machine),
            endpoint_url="",
        )
        return driver.logs(handle, tail=2000) or ""
    except Exception:
        return ""


@router.get("/policy-sessions/{session_id}/log", response_class=PlainTextResponse)
async def policy_session_log(
    session_id: int,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
):
    """The policy container's stdout+stderr: live from the container while the
    session runs, and the file captured at teardown once it has ended."""
    session = await db.get(PolicySession, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such session")
    if not session.terminal and session.machine_id and session.container_name:
        machine = await db.get(Machine, session.machine_id)
        if machine is not None:
            live = await anyio.to_thread.run_sync(
                _live_workload_log, machine, session.container_name
            )
            if live:
                return live
    path = policy_log_path(get_settings().run_log_dir, session_id)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read()
    return "(no captured log yet — the policy container is still starting or has left none)"


@router.post("/policy-sessions/{session_id}/abort", response_model=SessionOut)
async def abort_session(
    session_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_session),
):
    session = await db.get(PolicySession, session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such session")
    if session.terminal:
        raise HTTPException(status.HTTP_409_CONFLICT, f"session is already {session.status}")
    if session.abort_requested_at is None:
        session.abort_requested_at = datetime.now(UTC)
        db.add(
            Event(
                actor=user.username, kind="policy_session_abort_requested",
                campaign_id=session.campaign_id, payload={"session_id": session.id},
            )
        )
    await db.commit()
    await db.refresh(session)
    return SessionOut.model_validate(session)
