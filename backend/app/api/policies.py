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
from app.control.orchestrator.lifecycle import as_utc
from app.control.orchestrator.policy_lifecycle import deadlines, token_expiry
from app.control.orchestrator.policy_session import policy_log_path
from app.control.search.coverage import space_coverage
from app.core import apikeys
from app.core.auth import Principal, get_current_user, get_principal
from app.core.config import get_settings
from app.db.base import get_async_session
from app.db.models import (
    TERMINAL_SESSION_STATES,
    ApiKey,
    Campaign,
    Event,
    LlmEndpoint,
    Machine,
    Policy,
    PolicyContender,
    PolicySession,
    PolicySessionStatus,
    PolicyTrial,
    User,
)
from app.schemas.policy import (
    ContenderOut,
    PolicyIn,
    PolicyOut,
    SessionDetailOut,
    SessionOut,
    SessionTokenOut,
    SpaceCoverage,
    TrialOut,
)

router = APIRouter(tags=["policies"])


# -- registry -----------------------------------------------------------------


@router.get("/policies", response_model=list[PolicyOut])
async def list_policies(
    _principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_async_session),
):
    rows = (await db.execute(select(Policy).order_by(Policy.name))).scalars().all()
    return [PolicyOut.model_validate(row) for row in rows]


@router.get("/policies/defaults")
async def policy_defaults(_user: User = Depends(get_current_user)):
    """What a policy campaign gets when its settings leave a knob unset — for
    the campaign form to show before anyone overrides it."""
    from app.control.search.space import DEFAULT_FORBIDDEN_PARAMS

    return {"forbidden_params": list(DEFAULT_FORBIDDEN_PARAMS)}


async def _check_endpoint(db: AsyncSession, endpoint_id: int | None) -> None:
    if endpoint_id is not None and await db.get(LlmEndpoint, endpoint_id) is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"no LLM endpoint {endpoint_id}"
        )


@router.post("/policies", response_model=PolicyOut, status_code=status.HTTP_201_CREATED)
async def create_policy(
    body: PolicyIn,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_async_session),
):
    # A service key may register one too, as the install guide does.
    user = principal.user
    existing = (
        await db.execute(select(Policy).where(Policy.name == body.name))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"policy '{body.name}' already exists")
    await _check_endpoint(db, body.llm_endpoint_id)
    policy = Policy(owner_id=user.id, **body.model_dump(exclude={"secret_env"}))
    policy.set_secret_env(body.secret_env or {})
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
    await _check_endpoint(db, body.llm_endpoint_id)
    for key, value in body.model_dump(exclude={"secret_env"}).items():
        setattr(policy, key, value)
    if body.secret_env is not None:
        policy.set_secret_env(body.secret_env)
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


@router.post(
    "/campaigns/{campaign_id}/session-token",
    response_model=SessionTokenOut,
    summary="The token of an external policy's live session",
)
async def session_token(
    campaign_id: int,
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_async_session),
):
    """Issue the credential an external policy speaks the policy API with.

    A policy the platform runs is handed its token in its container's
    environment. An external one (`Policy.external`) runs elsewhere, so the
    campaign's owner asks for the token here, once the session has its
    machine. The token is shown this once and never stored; asking again
    issues a new one and ends the one before, so a policy that lost its
    token gets back in and a leaked one is closed by the same call.
    """
    user = principal.user
    campaign = await db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such campaign")
    if campaign.owner_id != user.id and user.role != "admin":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "only the campaign's owner or an admin can ask"
        )
    policy = await db.get(Policy, campaign.policy_id) if campaign.policy_id else None
    if policy is None or not policy.external:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this campaign's policy is not external: the platform runs it and "
            "gives its container the token",
        )
    session = (
        await db.execute(
            select(PolicySession)
            .where(
                PolicySession.campaign_id == campaign.id,
                PolicySession.status.not_in([s.value for s in TERMINAL_SESSION_STATES]),
            )
            .order_by(PolicySession.id.desc())
        )
    ).scalars().first()
    if session is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "no live session: the campaign is not running, or its window has not opened",
        )
    if session.machine_id is None or session.status == PolicySessionStatus.PENDING.value:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"session {session.id} is waiting for a machine; ask again once it has one",
        )
    machine = await db.get(Machine, session.machine_id)
    moment = datetime.now(UTC)
    for key in (
        await db.execute(select(ApiKey).where(ApiKey.policy_session_id == session.id))
    ).scalars():
        expires = as_utc(key.expires_at)
        if expires is None or expires > moment:
            key.expires_at = moment
    settings = get_settings()
    token, prefix, key_hash = apikeys.mint()
    expires_at = token_expiry(deadlines(campaign, machine)[1], settings)
    db.add(
        ApiKey(
            name=f"policy-session-{session.id}",
            prefix=prefix,
            key_hash=key_hash,
            owner_id=campaign.owner_id,
            policy_session_id=session.id,
            expires_at=expires_at,
        )
    )
    db.add(
        Event(
            actor=principal.actor, kind="policy_session_token_issued",
            campaign_id=campaign.id, payload={"session_id": session.id},
        )
    )
    await db.commit()
    return SessionTokenOut(
        session_id=session.id, token=token, expires_at=expires_at,
        api_url=settings.public_api_url or "",
    )


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
    if (session.status == PolicySessionStatus.STARTING.value and machine is not None
            and session.container_name):
        detail.waiting = await anyio.to_thread.run_sync(
            _live_waiting, machine, session.container_name
        )
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


def _policy_handle(machine: Machine, container_name: str):
    """The driver for the machine's own cluster, and a handle on its policy
    container: marked a workload, so on a cluster it selects the policy's pod
    and never an engine's."""
    driver = get_driver(machine.driver or "ssh_docker", cluster_id=machine.cluster_id)
    handle = DeploymentHandle(
        driver=getattr(driver, "name", ""),
        container_name=container_name,
        machine=MachineInfo.of(machine),
        endpoint_url="",
        workload=True,
    )
    return driver, handle


def _live_workload_log(machine: Machine, container_name: str) -> str:
    """Best-effort read-only fetch of a running policy container's log (docker
    logs merges stdout+stderr). Any ssh hiccup returns empty, and the caller
    falls back to the captured file."""
    try:
        driver, handle = _policy_handle(machine, container_name)
        return driver.logs(handle, tail=2000) or ""
    except Exception:
        return ""


def _live_waiting(machine: Machine, container_name: str) -> str:
    """What a policy container that has not started is waiting on, as the
    substrate says it (a pod nothing can schedule yet, an image still pulling
    back off). Empty when nothing looks wrong or it cannot be read."""
    try:
        driver, handle = _policy_handle(machine, container_name)
        reason = driver.failure_reason(handle)
        return reason[1] if reason else ""
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
    if session.log_tail:
        return session.log_tail
    # Read when the container died, by a worker that kept nothing else.
    death_logs = (session.env_snapshot or {}).get("death_logs")
    if death_logs:
        return str(death_logs)
    if not session.terminal:
        return "(no log yet — the policy container is still starting)"
    if session.first_heartbeat_at is None:
        return (
            f"(No log: the policy container of session {session_id} never reached the "
            "platform, so it may never have started. Its error and the campaign's "
            "Activity log say why.)"
        )
    return (
        f"(No log was kept for session {session_id}: its container was gone before the "
        "platform could read it. The campaign's Activity log shows what the policy asked "
        "for and what the platform did.)"
    )


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
