"""The policy-facing API — the platform side of docs/api/policy-contract.md.

Everything here is called by third-party containers holding a session token
(core.auth.get_policy_session), never by browsers. Three rules shape every
handler:

  * Scoped by construction: the token resolves to ONE session, and every query
    filters on it. No handler trusts a campaign/machine id from the wire.
  * The API records and reads; the WORKER acts. A launch request becomes a
    PENDING Run the supervisor picks up next tick; a release becomes a
    timestamp it honours; `policy_sessions.status` is never written here.
  * Nothing a policy sends is silently rewritten. Configs are canonicalized
    (base merged, inactive params pruned) and echoed back; out-of-space
    parameters become recorded deviations, not rejections; the only 422s are
    safety (the same validate_config the platform's own expansion faces).
"""

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.control.orchestrator.policy_lifecycle import (
    PENDING_CONTENDER_STATES,
    command_for,
    deadlines,
    settings_of,
)
from app.control.search.base import CandidateConfig
from app.control.search.space import deviations as space_deviations
from app.control.search.space import prune_inactive
from app.control.search.validation import ValidationContext, cards_used, validate_config
from app.core.auth import PolicyCaller, get_policy_session
from app.core.config import get_settings
from app.db.base import get_async_session
from app.db.models import (
    TERMINAL_RUN_STATES,
    Campaign,
    Candidate,
    CandidateKind,
    CandidateStatus,
    ContenderStatus,
    Event,
    Machine,
    Policy,
    PolicyContender,
    PolicySession,
    PolicySessionStatus,
    PolicyState,
    PolicyTrial,
    Result,
    Run,
    RunKind,
    RunStatus,
)
from app.objective import direction, redlines, target_metric
from app.schemas.policy import (
    CONTRACT_VERSION,
    BenchmarkOut,
    BenchmarkSuite,
    BenchmarkSummary,
    ContenderOut,
    ContendersEcho,
    ContendersPut,
    ExternalBenchmarkIn,
    HeartbeatIn,
    HeartbeatOut,
    LaunchBenchmarkIn,
    LaunchIn,
    LaunchOut,
    ManifestContenders,
    ManifestHardware,
    ManifestModel,
    ManifestObjective,
    ManifestServices,
    ManifestTime,
    PlanIn,
    PolicyEventIn,
    ServingIn,
    SessionManifest,
    TrialIn,
    TrialOut,
)
from app.staging import SCREEN, VERIFY
from app.staging import objective as stage_objective

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/policy/v1", tags=["policy sessions"])

# Sessions still exploring: the states in which launches/benches are accepted.
_EXPLORING = {PolicySessionStatus.STARTING.value, PolicySessionStatus.SEARCHING.value}
# The nominal minutes one screening bench takes, for the "does it still fit
# before the search deadline" refusal. A bound would be max_run_minutes, but
# refusing on the bound would shut the night's tail hours early.
_SCREEN_APPROX_MINUTES = 15


# -- shared helpers -------------------------------------------------------------


async def _context(
    db: AsyncSession, caller: PolicyCaller
) -> tuple[PolicySession, Campaign, Machine | None]:
    session = caller.session
    campaign = await db.get(Campaign, session.campaign_id)
    if campaign is None:  # cannot happen without manual surgery; fail honestly
        raise HTTPException(status.HTTP_410_GONE, "Campaign no longer exists")
    machine = await db.get(Machine, session.machine_id) if session.machine_id else None
    return session, campaign, machine


def _mark_activity(session: PolicySession) -> None:
    """Record delegated work — the signal the productivity watchdog reads.

    A launch or benchmark request (even one the platform later rejects for
    capacity) proves the policy is still exploring, so it bumps the activity
    clock, clears any accrued idle strikes, and drops a stale "exhausted"
    signal: a policy asking for more work is, by its own action, not done.
    """
    session.last_activity_at = datetime.now(UTC)
    session.idle_strikes = 0
    session.last_idle_strike_at = None
    if session.policy_status in ("exhausted",):
        session.policy_status = ""


def _canonical(config: dict[str, Any], campaign: Campaign) -> dict[str, Any]:
    """Base merged in, conditionally-inactive params pruned — the exact
    normalization expand()/decode() apply, so a policy's config hashes the
    same as the identical config proposed by the platform itself."""
    space = campaign.search_space or {}
    merged = {**(space.get("base") or {}), **config}
    return prune_inactive(merged, space)


def _reject_placement(config: dict[str, Any]) -> None:
    from app.control.engine_command import PLACEMENT_FLAGS

    offending = sorted(set(config) & set(PLACEMENT_FLAGS))
    if offending:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"placement is the platform's: remove {', '.join(offending)}",
        )


def _validated(
    config: dict[str, Any], campaign: Campaign, machine: Machine | None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Canonical config + deviations, or a 422 for the safety failures."""
    _reject_placement(config)
    canonical = _canonical(config, campaign)
    error = validate_config(
        canonical,
        ValidationContext(
            gpu_count=(machine.gpu_count if machine else 0) or 0, engine=campaign.engine
        ),
    )
    if error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, error)
    return canonical, space_deviations(canonical, campaign.search_space or {})


async def _live_session_runs(db: AsyncSession, session: PolicySession) -> list[Run]:
    return list(
        (
            await db.execute(
                select(Run).where(
                    Run.policy_session_id == session.id,
                    Run.status.not_in([s.value for s in TERMINAL_RUN_STATES]),
                )
            )
        )
        .scalars()
        .all()
    )


def _require_exploring(session: PolicySession, campaign: Campaign, machine) -> None:
    if session.status not in _EXPLORING:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"session is {session.status}; exploration is over — follow the heartbeat",
        )
    search, _ = deadlines(campaign, machine)
    if search is not None and datetime.now(UTC) >= search:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "past the search deadline — finalize and register contenders",
        )


async def _idempotent_run(
    db: AsyncSession, session: PolicySession, key: str | None
) -> Run | None:
    if not key:
        return None
    return (
        await db.execute(
            select(Run).where(
                Run.policy_session_id == session.id, Run.idempotency_key == key
            )
        )
    ).scalar_one_or_none()


def _check_cards_and_port(
    session: PolicySession,
    live: list[Run],
    gpu_indices: list[int],
    port: int | None,
) -> int:
    """Enforce the allocation: cards/ports from the manifest only, no overlap
    with the session's own live runs. Returns the port to use."""
    allowed = set(session.gpu_indices or [])
    asked = set(gpu_indices)
    if not asked <= allowed:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"cards {sorted(asked - allowed)} are outside this session's allocation "
            f"{sorted(allowed)}",
        )
    busy_cards = {i for r in live for i in (r.gpu_indices or [])}
    if asked & busy_cards:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"cards {sorted(asked & busy_cards)} are busy with your own runs — "
            "release something or wait",
        )
    session_ports = list(session.ports or [])
    busy_ports = {r.service_port for r in live if r.service_port}
    if port is not None:
        if port not in session_ports:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"port {port} is outside this session's allocation {session_ports}",
            )
        if port in busy_ports:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS, f"port {port} is busy with your own runs"
            )
        return port
    for candidate_port in session_ports:
        if candidate_port not in busy_ports:
            return candidate_port
    raise HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS, "no free port in this session's allocation"
    )


async def _make_candidate(
    db: AsyncSession,
    campaign: Campaign,
    config: dict[str, Any],
    dev: list[dict[str, Any]],
    kind: str = CandidateKind.SEARCH.value,
) -> Candidate:
    candidate = Candidate(
        campaign_id=campaign.id,
        config=config,
        config_hash=CandidateConfig(engine_args=config).hash,
        # EXHAUSTED from birth: these rows exist for the ledger (results,
        # leaderboard, history), not for the scheduler to pick up.
        status=CandidateStatus.EXHAUSTED.value,
        kind=kind,
        deviations=dev or None,
    )
    db.add(candidate)
    await db.flush()
    return candidate


def _launch_out(run: Run, candidate: Candidate | None = None) -> LaunchOut:
    mapping = {
        RunStatus.PENDING.value: "queued",
        RunStatus.LAUNCHING.value: "launching",
        RunStatus.WAITING_READY.value: "launching",
        RunStatus.HEALTH_CHECK.value: "launching",
        RunStatus.SERVING.value: "serving",
        RunStatus.SUCCEEDED.value: "released",
        RunStatus.FAILED.value: "failed",
        RunStatus.KILLED.value: "failed",
    }
    state = mapping.get(run.status, run.status)
    if run.status == RunStatus.SERVING.value and run.release_requested_at is not None:
        state = "releasing"
    return LaunchOut(
        id=run.id,
        status=state,
        endpoint_url=run.endpoint_url or "",
        launch_command=run.launch_command or "",
        failure_class=run.failure_class or "",
        error=(run.error or "")[:2000],
        config=(candidate.config if candidate else {}),
        deviations=list((candidate.deviations if candidate else None) or []),
    )


async def _benchmark_out(db: AsyncSession, run: Run, candidate: Candidate | None) -> BenchmarkOut:
    mapping = {
        RunStatus.PENDING.value: "queued",
        RunStatus.HEALTH_CHECK.value: "health_check",
        RunStatus.BENCHING.value: "benching",
        RunStatus.SUCCEEDED.value: "succeeded",
        RunStatus.FAILED.value: "failed",
        RunStatus.KILLED.value: "killed",
    }
    summary = None
    metrics: dict[str, Any] = {}
    result = (
        await db.execute(
            select(Result)
            .where(Result.run_id == run.id, Result.source == "llmbench")
            .order_by(Result.id.desc())
        )
    ).scalars().first()
    if result is not None:
        summary = BenchmarkSummary(
            objective_value=result.objective_value,
            feasible=bool(result.feasible),
            constraints=list(result.constraints or []),
            breaches=list(result.breaches or []),
        )
        metrics = dict(result.metrics or {})
    from app.staging import stage_of

    return BenchmarkOut(
        id=run.id,
        status=mapping.get(run.status, run.status),
        suite=VERIFY if candidate is not None and stage_of(candidate) == VERIFY else SCREEN,
        summary=summary,
        metrics=metrics,
        failure_class=run.failure_class or "",
        error=(run.error or "")[:2000],
        config=(candidate.config if candidate else {}),
        deviations=list((candidate.deviations if candidate else None) or []),
    )


def _audit(
    db: AsyncSession,
    caller: PolicyCaller,
    kind: str,
    payload: dict[str, Any] | None = None,
    run_id: int | None = None,
) -> None:
    db.add(
        Event(
            actor=caller.actor,
            kind=kind,
            campaign_id=caller.session.campaign_id,
            run_id=run_id,
            payload={"session_id": caller.session.id, **(payload or {})},
        )
    )


# -- the manifest ----------------------------------------------------------------


@router.get("/session", response_model=SessionManifest)
async def manifest(
    caller: PolicyCaller = Depends(get_policy_session),
    db: AsyncSession = Depends(get_async_session),
):
    session, campaign, machine = await _context(db, caller)
    policy = await db.get(Policy, session.policy_id)
    settings = get_settings()
    knobs = settings_of(campaign)
    search, hard = deadlines(campaign, machine)

    def _objective(stage: str) -> ManifestObjective:
        objective = stage_objective(campaign, stage)
        return ManifestObjective(
            target_metric=target_metric(objective),
            direction=direction(objective),
            redlines=redlines(objective),
        )

    prior_contenders = (
        (
            await db.execute(
                select(PolicyContender)
                .where(
                    PolicyContender.campaign_id == campaign.id,
                    PolicyContender.session_id != session.id,
                    PolicyContender.status.in_(
                        [ContenderStatus.VALIDATED.value, ContenderStatus.FAILED.value]
                    ),
                )
                .order_by(PolicyContender.id.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    prior = {
        "contenders": [
            {
                "launch_spec": c.launch_spec,
                "config_hash": c.config_hash,
                "status": c.status,
                "evidence": c.evidence,
                "run_id": c.run_id,
            }
            for c in prior_contenders
        ]
    }

    production = {}
    if machine is not None:
        for service in (machine.baseline or {}).get("services", []):
            if service.get("served_model_name") == campaign.served_model_name:
                production = {
                    "engine_args": service.get("engine_args") or {},
                    "launch_command": service.get("docker_run") or "",
                }
                break

    screen_slug = campaign.benchmark_slug or settings.llmbench_benchmark_slug
    return SessionManifest(
        contract={"version": CONTRACT_VERSION},
        session_id=session.id,
        campaign_id=campaign.id,
        policy={
            "name": policy.name if policy else "",
            "image": policy.image if policy else "",
            "version": policy.version if policy else "",
        },
        model=ManifestModel(
            engine=campaign.engine,
            image=campaign.image,
            model_path=campaign.model_path,
            served_model_name=campaign.served_model_name,
            extra_env=campaign.extra_env or {},
            extra_volumes=campaign.extra_volumes or {},
        ),
        hardware=ManifestHardware(
            machine=machine.name if machine else "",
            gpu_type=(machine.gpu_type if machine else "") or "",
            gpu_indices=list(session.gpu_indices or []),
            ports=list(session.ports or []),
            gpus_visible_in_container=bool(
                (await db.get(Policy, session.policy_id)).gpus_in_container
                if policy
                else True
            ),
        ),
        objective={SCREEN: _objective(SCREEN), VERIFY: _objective(VERIFY)},
        search_space=campaign.search_space or {},
        production=production,
        time=ManifestTime(
            started_at=session.started_at,
            search_deadline=search,
            hard_deadline=hard,
            heartbeat_interval_s=settings.policy_heartbeat_interval_seconds,
            heartbeat_timeout_s=knobs.heartbeat_timeout_s
            or settings.policy_heartbeat_timeout_seconds,
            tick_s=settings.worker_tick_seconds,
        ),
        contenders=ManifestContenders(
            max=knobs.max_contenders,
            validation_suite=VERIFY,
            approx_minutes_each=knobs.approx_minutes_each,
        ),
        services=ManifestServices(
            launch=True,
            benchmarks={
                SCREEN: BenchmarkSuite(
                    slug=screen_slug,
                    approx_minutes=_SCREEN_APPROX_MINUTES,
                    dataset_build_id=campaign.dataset_build_id or "",
                )
            },
            state=True,
        ),
        prior=prior,
    )


# -- heartbeat / lifecycle ---------------------------------------------------------


@router.post("/session/heartbeat", response_model=HeartbeatOut)
async def heartbeat(
    body: HeartbeatIn,
    caller: PolicyCaller = Depends(get_policy_session),
    db: AsyncSession = Depends(get_async_session),
):
    session, campaign, machine = await _context(db, caller)
    now = datetime.now(UTC)
    session.last_heartbeat_at = now
    if session.first_heartbeat_at is None:
        session.first_heartbeat_at = now
        session.capabilities = list(body.capabilities or [])
        session.contract_version = body.contract_version or ""
        session.sdk_version = body.sdk_version or ""
    if body.phase:
        session.policy_phase = body.phase[:32]
    if body.message:
        session.policy_message = body.message[:4000]
    if body.progress is not None:
        session.policy_progress = body.progress
    # Explicit lifecycle signal (v1.1). Only the two terminal signals are
    # acted on; a later empty status must NOT clear a standing one — once a
    # policy has said it is done, the winding-down heartbeats it then sends
    # (phase "validating", status "") would otherwise erase the signal before
    # the worker records why search ended, mislabelling it. The signal is
    # cleared only by real delegated work (_mark_activity), i.e. the policy
    # actually resuming — which is the one case where "not done after all" is true.
    if body.status in ("exhausted", "error"):
        session.policy_status = body.status
        if body.status == "error" and body.failure_class:
            session.failure_class = session.failure_class or body.failure_class[:64]
    if body.coverage is not None:
        session.coverage = body.coverage.model_dump(mode="json")

    pending = (
        await db.execute(
            select(func.count(PolicyContender.id)).where(
                PolicyContender.session_id == session.id,
                PolicyContender.status.in_(list(PENDING_CONTENDER_STATES)),
            )
        )
    ).scalar_one()
    ports = list(session.ports or [])
    command = command_for(
        session, campaign, machine, pending, serving_port=ports[0] if ports else None
    )
    await db.commit()
    return HeartbeatOut(**command)


@router.post("/session/finalized", status_code=status.HTTP_204_NO_CONTENT)
async def finalized(
    caller: PolicyCaller = Depends(get_policy_session),
    db: AsyncSession = Depends(get_async_session),
):
    session = caller.session
    if session.finalized_at is None:
        session.finalized_at = datetime.now(UTC)
        _audit(db, caller, "policy_finalized")
    await db.commit()


@router.put("/session/plan", status_code=status.HTTP_204_NO_CONTENT)
async def put_plan(
    body: PlanIn,
    caller: PolicyCaller = Depends(get_policy_session),
    db: AsyncSession = Depends(get_async_session),
):
    session = caller.session
    session.plan = body.model_dump(mode="json")
    _audit(db, caller, "policy_plan_updated", payload={"plan": session.plan})
    await db.commit()


# -- trials ----------------------------------------------------------------------


@router.post("/trials", response_model=TrialOut, status_code=status.HTTP_201_CREATED)
async def report_trial(
    body: TrialIn,
    caller: PolicyCaller = Depends(get_policy_session),
    db: AsyncSession = Depends(get_async_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    session, campaign, _machine = await _context(db, caller)
    if idempotency_key:
        existing = (
            await db.execute(
                select(PolicyTrial).where(
                    PolicyTrial.session_id == session.id,
                    PolicyTrial.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return TrialOut.model_validate(existing)
    canonical = _canonical(body.config, campaign)
    count = (
        await db.execute(
            select(func.count(PolicyTrial.id)).where(PolicyTrial.session_id == session.id)
        )
    ).scalar_one()
    trial = PolicyTrial(
        session_id=session.id,
        campaign_id=campaign.id,
        seq=count + 1,
        config=canonical,
        config_hash=CandidateConfig(engine_args=canonical).hash,
        source="self",  # a policy can only ever claim its own measurements
        reported_metrics=body.reported_metrics,
        reported_objective_value=body.reported_objective_value,
        reported_feasible=body.reported_feasible,
        failure_class=body.failure_class[:64],
        duration_seconds=body.duration_seconds,
        notes=body.notes[:4000],
        idempotency_key=idempotency_key,
    )
    db.add(trial)
    await db.commit()
    await db.refresh(trial)
    return TrialOut.model_validate(trial)


@router.get("/trials", response_model=list[TrialOut])
async def list_trials(
    caller: PolicyCaller = Depends(get_policy_session),
    db: AsyncSession = Depends(get_async_session),
):
    rows = (
        (
            await db.execute(
                select(PolicyTrial)
                .where(PolicyTrial.session_id == caller.session.id)
                .order_by(PolicyTrial.seq)
            )
        )
        .scalars()
        .all()
    )
    return [TrialOut.model_validate(row) for row in rows]


# -- delegated launches --------------------------------------------------------------


@router.post("/launches", response_model=LaunchOut, status_code=status.HTTP_201_CREATED)
async def request_launch(
    body: LaunchIn,
    caller: PolicyCaller = Depends(get_policy_session),
    db: AsyncSession = Depends(get_async_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    session, campaign, machine = await _context(db, caller)
    _require_exploring(session, campaign, machine)
    _mark_activity(session)
    existing = await _idempotent_run(db, session, idempotency_key)
    if existing is not None:
        await db.commit()
        return _launch_out(existing, await db.get(Candidate, existing.candidate_id))

    canonical, dev = _validated(body.engine_args, campaign, machine)
    needed = cards_used(canonical)
    if needed != len(body.gpu_indices):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"config uses {needed} card(s) (tp×dp×pp) but {len(body.gpu_indices)} were "
            "requested — they must match",
        )
    live = await _live_session_runs(db, session)
    port = _check_cards_and_port(session, live, body.gpu_indices, body.port)

    candidate = await _make_candidate(db, campaign, canonical, dev)
    run = Run(
        campaign_id=campaign.id,
        candidate_id=candidate.id,
        machine_id=session.machine_id,
        kind=RunKind.POLICY_LAUNCH.value,
        status=RunStatus.PENDING.value,
        gpu_indices=list(body.gpu_indices),
        service_port=port,
        policy_session_id=session.id,
        idempotency_key=idempotency_key,
        # Stamp the clock at creation like the classic scheduler does, so every
        # run kind carries a start time and duration/timeline readers never see
        # a null on the policy paths.
        started_at=datetime.now(UTC),
    )
    db.add(run)
    await db.flush()
    _audit(
        db, caller, "policy_launch_requested", run_id=run.id,
        payload={"config": canonical, "gpus": body.gpu_indices, "port": port},
    )
    await db.commit()
    return _launch_out(run, candidate)


@router.get("/launches/{run_id}", response_model=LaunchOut)
async def launch_status(
    run_id: int,
    caller: PolicyCaller = Depends(get_policy_session),
    db: AsyncSession = Depends(get_async_session),
):
    run = await _session_run(db, caller, run_id, RunKind.POLICY_LAUNCH.value)
    return _launch_out(run, await db.get(Candidate, run.candidate_id))


@router.delete("/launches/{run_id}", response_model=LaunchOut)
async def release_launch(
    run_id: int,
    caller: PolicyCaller = Depends(get_policy_session),
    db: AsyncSession = Depends(get_async_session),
):
    run = await _session_run(db, caller, run_id, RunKind.POLICY_LAUNCH.value)
    if run.status not in {s.value for s in TERMINAL_RUN_STATES} and (
        run.release_requested_at is None
    ):
        run.release_requested_at = datetime.now(UTC)
        _audit(db, caller, "policy_release_requested", run_id=run.id)
    await db.commit()
    return _launch_out(run, await db.get(Candidate, run.candidate_id))


async def _session_run(
    db: AsyncSession, caller: PolicyCaller, run_id: int, kind: str | None = None
) -> Run:
    run = (
        await db.execute(
            select(Run).where(Run.id == run_id, Run.policy_session_id == caller.session.id)
        )
    ).scalar_one_or_none()
    if run is None or (kind is not None and run.kind != kind):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such run in this session")
    return run


# -- benchmarks ------------------------------------------------------------------------


@router.post(
    "/launches/{run_id}/benchmarks",
    response_model=BenchmarkOut,
    status_code=status.HTTP_201_CREATED,
)
async def bench_launch(
    run_id: int,
    body: LaunchBenchmarkIn,
    caller: PolicyCaller = Depends(get_policy_session),
    db: AsyncSession = Depends(get_async_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    session, campaign, machine = await _context(db, caller)
    _require_exploring(session, campaign, machine)
    _require_known_suite(body.suite)
    _mark_activity(session)
    parent = await _session_run(db, caller, run_id, RunKind.POLICY_LAUNCH.value)
    if parent.status != RunStatus.SERVING.value:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"launch {parent.id} is {parent.status}, not serving — wait for it",
        )
    existing = await _idempotent_run(db, session, idempotency_key)
    if existing is not None:
        await db.commit()
        return await _benchmark_out(db, existing, await db.get(Candidate, existing.candidate_id))
    run = Run(
        campaign_id=campaign.id,
        candidate_id=parent.candidate_id,  # same config, same ledger entry
        machine_id=session.machine_id,
        kind=RunKind.EXTERNAL.value,
        status=RunStatus.HEALTH_CHECK.value,
        endpoint_url=parent.endpoint_url,
        gpu_indices=[],  # the cards belong to the parent
        service_port=0,
        policy_session_id=session.id,
        idempotency_key=idempotency_key,
        started_at=datetime.now(UTC),
    )
    db.add(run)
    await db.flush()
    _audit(db, caller, "policy_bench_requested", run_id=run.id, payload={"of_launch": parent.id})
    await db.commit()
    return await _benchmark_out(db, run, await db.get(Candidate, run.candidate_id))


@router.post("/benchmarks", response_model=BenchmarkOut, status_code=status.HTTP_201_CREATED)
async def bench_external(
    body: ExternalBenchmarkIn,
    caller: PolicyCaller = Depends(get_policy_session),
    db: AsyncSession = Depends(get_async_session),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    session, campaign, machine = await _context(db, caller)
    _require_exploring(session, campaign, machine)
    _require_known_suite(body.suite)
    _mark_activity(session)
    if machine is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "session has no machine")
    if body.port not in (session.ports or []):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"port {body.port} is outside this session's allocation {session.ports}",
        )
    existing = await _idempotent_run(db, session, idempotency_key)
    if existing is not None:
        await db.commit()
        return await _benchmark_out(db, existing, await db.get(Candidate, existing.candidate_id))

    _require_self_serving_is_reachable(machine)
    canonical, dev = _validated(body.engine_args, campaign, machine)
    candidate = await _make_candidate(db, campaign, canonical, dev)
    run = Run(
        campaign_id=campaign.id,
        candidate_id=candidate.id,
        machine_id=session.machine_id,
        kind=RunKind.EXTERNAL.value,
        status=RunStatus.HEALTH_CHECK.value,
        # Built from OUR machine row, never from a caller-supplied URL: a
        # session can only get things on its own box measured.
        endpoint_url=f"http://{machine.host}:{body.port}",
        gpu_indices=list(body.gpu_indices),
        service_port=body.port,
        policy_session_id=session.id,
        idempotency_key=idempotency_key,
        started_at=datetime.now(UTC),
    )
    db.add(run)
    await db.flush()
    _audit(
        db, caller, "policy_bench_requested", run_id=run.id,
        payload={"endpoint": run.endpoint_url, "config": canonical},
    )
    await db.commit()
    return await _benchmark_out(db, run, candidate)


def _require_self_serving_is_reachable(machine) -> None:
    """Refuse a self-served benchmark on a substrate where the policy's own
    engine has no address.

    `POST /benchmarks` measures something the POLICY is serving itself, so the
    endpoint is built from the machine's host and the session's port. That
    works on ssh_docker, where the policy container runs with `--network host`
    and its engine binds a real port on a real box.

    On k8s neither half holds: `machines.host` is unused (placement is the
    scheduler's job, and SETUP.md says to leave it blank), and a policy pod is
    a Job with no Service, so nothing outside the pod can reach it — the URL
    would come out as `http://:28200` and fail much later, inside LLMBench, as
    an unreachable endpoint. Say so here instead.

    `POST /launches/{id}/benchmarks` is unaffected and is the path a delegated
    policy uses: it measures a platform-launched engine and takes its endpoint
    from the parent run, which the k8s driver resolves from the operator.
    """
    if not (machine.host or "").strip():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"machine {machine.name} has no reachable host address, so a "
            "self-served benchmark has no endpoint to measure — this is a "
            "cluster machine, where the policy container is not reachable from "
            "outside its pod. Launch through POST /launches and benchmark it "
            "with POST /launches/{id}/benchmarks instead.",
        )


def _require_known_suite(suite: str) -> None:
    if suite != SCREEN:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"unknown suite '{suite}' — tonight's menu offers: {SCREEN}",
        )


@router.get("/benchmarks/{run_id}", response_model=BenchmarkOut)
async def benchmark_status(
    run_id: int,
    caller: PolicyCaller = Depends(get_policy_session),
    db: AsyncSession = Depends(get_async_session),
):
    run = await _session_run(db, caller, run_id, RunKind.EXTERNAL.value)
    return await _benchmark_out(db, run, await db.get(Candidate, run.candidate_id))


@router.delete("/benchmarks/{run_id}", response_model=BenchmarkOut)
async def cancel_benchmark(
    run_id: int,
    caller: PolicyCaller = Depends(get_policy_session),
    db: AsyncSession = Depends(get_async_session),
):
    run = await _session_run(db, caller, run_id, RunKind.EXTERNAL.value)
    if run.status not in {s.value for s in TERMINAL_RUN_STATES}:
        # The same channel a human's stop button uses; the worker cancels the
        # submission and kills the run on its next tick.
        _audit(db, caller, "stop_requested", run_id=run.id)
        db.add(
            Event(
                actor=caller.actor, kind="stop_requested",
                campaign_id=run.campaign_id, run_id=run.id, payload={},
            )
        )
    await db.commit()
    return await _benchmark_out(db, run, await db.get(Candidate, run.candidate_id))


# -- contenders ---------------------------------------------------------------------


@router.put("/contenders", response_model=ContendersEcho)
async def put_contenders(
    body: ContendersPut,
    caller: PolicyCaller = Depends(get_policy_session),
    db: AsyncSession = Depends(get_async_session),
):
    session, campaign, machine = await _context(db, caller)
    if session.status not in _EXPLORING:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"contenders are frozen once the session is {session.status}",
        )
    knobs = settings_of(campaign)
    accepted_rows: list[PolicyContender] = []
    rejected: list[dict[str, Any]] = []
    seen_ranks: set[int] = set()
    for entry in body.contenders[: knobs.max_contenders * 2]:
        if entry.rank in seen_ranks:
            rejected.append({"rank": entry.rank, "reason": "duplicate rank"})
            continue
        seen_ranks.add(entry.rank)
        if len(accepted_rows) >= knobs.max_contenders:
            rejected.append(
                {"rank": entry.rank, "reason": f"over the campaign cap of {knobs.max_contenders}"}
            )
            continue
        try:
            canonical, dev = _validated(entry.launch_spec.engine_args, campaign, machine)
        except HTTPException as exc:
            rejected.append({"rank": entry.rank, "reason": str(exc.detail)})
            continue
        spec = entry.launch_spec.model_dump(mode="json")
        spec["engine_args"] = canonical
        accepted_rows.append(
            PolicyContender(
                session_id=session.id,
                campaign_id=campaign.id,
                rank=entry.rank,
                launch_spec=spec,
                config_hash=CandidateConfig(engine_args=canonical).hash,
                status=ContenderStatus.REGISTERED.value,
                evidence=entry.evidence,
                trial_ids=entry.trial_ids,
                deviations=dev or None,
            )
        )
    if not accepted_rows and body.contenders:
        # Refusing the whole update leaves the previous list standing, which
        # is the safer night: better an old best-so-far than none.
        return ContendersEcho(accepted=[], rejected=rejected)

    current = (
        await db.execute(
            select(PolicyContender).where(
                PolicyContender.session_id == session.id,
                PolicyContender.status == ContenderStatus.REGISTERED.value,
            )
        )
    ).scalars().all()

    # Idempotent no-op: a policy that re-declares the same best on a cadence
    # (rank + config unchanged) should not churn the ledger — no supersede, no
    # new rows, no event. Keeps the contender history to real changes.
    def _sig(rows: list[PolicyContender]) -> list[tuple[int, str]]:
        return sorted((r.rank, r.config_hash) for r in rows)

    if _sig(current) == _sig(accepted_rows):
        return ContendersEcho(
            accepted=[ContenderOut.model_validate(r) for r in current], rejected=rejected
        )

    for row in current:
        row.status = ContenderStatus.SUPERSEDED.value
    for row in accepted_rows:
        db.add(row)
    await db.flush()
    _audit(
        db, caller, "policy_contenders_updated",
        payload={"count": len(accepted_rows), "rejected": len(rejected)},
    )
    await db.commit()
    return ContendersEcho(
        accepted=[ContenderOut.model_validate(r) for r in accepted_rows], rejected=rejected
    )


@router.get("/contenders", response_model=list[ContenderOut])
async def list_contenders(
    caller: PolicyCaller = Depends(get_policy_session),
    db: AsyncSession = Depends(get_async_session),
):
    rows = (
        (
            await db.execute(
                select(PolicyContender)
                .where(PolicyContender.session_id == caller.session.id)
                .order_by(PolicyContender.id)
            )
        )
        .scalars()
        .all()
    )
    return [ContenderOut.model_validate(r) for r in rows]


@router.post("/contenders/{contender_id}/serving", response_model=ContenderOut)
async def contender_serving(
    contender_id: int,
    body: ServingIn,
    caller: PolicyCaller = Depends(get_policy_session),
    db: AsyncSession = Depends(get_async_session),
):
    session, campaign, machine = await _context(db, caller)
    if session.status != PolicySessionStatus.VALIDATING.value:
        raise HTTPException(status.HTTP_409_CONFLICT, "not validating yet — follow the heartbeat")
    if session.serving_contender_id != contender_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"the heartbeat commanded contender {session.serving_contender_id}, "
            f"not {contender_id} — serve exactly what was asked",
        )
    if machine is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "session has no machine")
    if body.port not in (session.ports or []):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"port {body.port} is outside this session's allocation {session.ports}",
        )
    contender = (
        await db.execute(
            select(PolicyContender).where(
                PolicyContender.id == contender_id,
                PolicyContender.session_id == session.id,
            )
        )
    ).scalar_one_or_none()
    if contender is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such contender in this session")
    if contender.run_id is not None:  # duplicate /serving for the same command
        return ContenderOut.model_validate(contender)

    spec = contender.launch_spec or {}
    config = dict(spec.get("engine_args") or {})
    candidate = await _make_candidate(
        db, campaign, config, list(contender.deviations or []),
        kind=CandidateKind.VERIFICATION.value,
    )
    run = Run(
        campaign_id=campaign.id,
        candidate_id=candidate.id,
        machine_id=session.machine_id,
        kind=RunKind.EXTERNAL.value,
        status=RunStatus.HEALTH_CHECK.value,
        endpoint_url=f"http://{machine.host}:{body.port}",
        gpu_indices=list(session.gpu_indices or []),
        service_port=body.port,
        policy_session_id=session.id,
        started_at=datetime.now(UTC),
        # The policy's declared provenance, stamped with what we know
        # ourselves. `image_tag` is what promotion pins by, and it is the
        # ENGINE image from the spec — never the policy container's.
        env_snapshot={
            **(spec.get("environment") or {}),
            "image_tag": spec.get("image") or "",
            "machine_gpu_type": machine.gpu_type or "",
            "card_type": machine.gpu_type or "",
            "gpu_indices": list(session.gpu_indices or []),
        },
    )
    db.add(run)
    await db.flush()
    contender.status = ContenderStatus.SERVING.value
    contender.served_port = body.port
    contender.candidate_id = candidate.id
    contender.run_id = run.id
    _audit(
        db, caller, "policy_contender_serving", run_id=run.id,
        payload={"contender_id": contender.id, "port": body.port},
    )
    await db.commit()
    return ContenderOut.model_validate(contender)


# -- state -------------------------------------------------------------------------


@router.get("/state")
async def get_state(
    caller: PolicyCaller = Depends(get_policy_session),
    db: AsyncSession = Depends(get_async_session),
):
    row = await db.get(PolicyState, caller.session.campaign_id)
    if row is None or not row.size:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No state stored for this campaign")
    return Response(content=row.blob, media_type=row.content_type)


@router.put("/state", status_code=status.HTTP_204_NO_CONTENT)
async def put_state(
    request: Request,
    caller: PolicyCaller = Depends(get_policy_session),
    db: AsyncSession = Depends(get_async_session),
):
    blob = await request.body()
    limit = get_settings().policy_state_max_bytes
    if len(blob) > limit:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"state is {len(blob)} bytes; the cap is {limit}",
        )
    content_type = request.headers.get("content-type", "application/octet-stream")
    row = await db.get(PolicyState, caller.session.campaign_id)
    if row is None:
        row = PolicyState(campaign_id=caller.session.campaign_id)
        db.add(row)
    row.blob = blob
    row.content_type = content_type[:128]
    row.size = len(blob)
    await db.commit()


# -- events -------------------------------------------------------------------------


@router.post("/events", status_code=status.HTTP_204_NO_CONTENT)
async def post_event(
    body: PolicyEventIn,
    caller: PolicyCaller = Depends(get_policy_session),
    db: AsyncSession = Depends(get_async_session),
):
    _audit(
        db, caller, "policy_event",
        payload={"level": body.level[:16], "message": body.message},
    )
    await db.commit()
