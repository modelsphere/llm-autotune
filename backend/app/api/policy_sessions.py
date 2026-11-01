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
    parameters become recorded deviations, not rejections. The only 422s are
    safety (the same validate_config the platform's own expansion faces), a
    forbidden parameter changed, and a value that cannot round-trip through a
    command line — all three answered in advance by POST /configs/check.
"""

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.loader import load_run
from app.agent.parts import environment_part, results_part
from app.control.baseline import production_for
from app.control.engine_command import PLACEMENT_FLAGS, round_trip_problems
from app.control.engines import vocabulary as vocab_mod
from app.control.engines.base import render_args
from app.control.engines.env_keys import env_problems, is_env_key, split_env
from app.control.engines.flags import known_flags, normalize_args
from app.control.launch.clusters import resolve_cluster
from app.control.orchestrator.policy_lifecycle import (
    PENDING_CONTENDER_STATES,
    command_for,
    contender_parts,
    deadlines,
    settings_of,
)
from app.control.recipe import Recipe
from app.control.recipe import shape as recipe_shape
from app.control.recipe.address import BadAddress
from app.control.recipe.address import parse as parse_address
from app.control.recipe.campaign import (
    base_recipe,
    config_cards,
    config_pod_cards,
    recipe_for,
    split_flat,
)
from app.control.recipe.check import config_problems as recipe_config_problems
from app.control.recipe.check import recipe_problems
from app.control.recipe.command import recipe_flags
from app.control.recipe.render import chart_commands
from app.control.search.base import CandidateConfig
from app.control.search.space import deviations as space_deviations
from app.control.search.space import forbidden_hits, prune_inactive
from app.control.search.validation import ValidationContext, validate_config
from app.core.auth import PolicyCaller, get_policy_session
from app.core.config import get_settings
from app.db.base import get_async_session
from app.db.models import (
    TERMINAL_RUN_STATES,
    TERMINAL_SESSION_STATES,
    Campaign,
    Candidate,
    CandidateKind,
    CandidateStatus,
    ContenderStatus,
    EngineVocabulary,
    Event,
    Machine,
    ModelConfig,
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
from app.hardware import is_node_pool
from app.measures import reader_for, scenarios_out
from app.measures.catalog import catalogue as measure_catalogue
from app.measures.workload import describe as describe_workload
from app.objective import direction, redlines, target_metric
from app.reporting.run_log import read_run_log
from app.schemas.agent import EnvironmentPart, ResultsPart
from app.schemas.policy import (
    CONTRACT_VERSION,
    BenchmarkOut,
    BenchmarkSuite,
    BenchmarkSummary,
    ConfigCheckIn,
    ConfigCheckOut,
    ConfigProblem,
    ContenderOut,
    ContendersEcho,
    ContendersPut,
    DigestIn,
    ExternalBenchmarkIn,
    HeartbeatIn,
    HeartbeatOut,
    HistoryConditions,
    HistoryEntry,
    HistoryVerdict,
    LaunchBenchmarkIn,
    LaunchIn,
    LaunchOut,
    ManifestContenders,
    ManifestHardware,
    ManifestModel,
    ManifestObjective,
    ManifestProduction,
    ManifestServices,
    ManifestTime,
    PlanIn,
    PolicyEventIn,
    ScenarioMeasures,
    ServingIn,
    SessionManifest,
    TrialIn,
    TrialOut,
    VocabularyOut,
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


async def _model_config(db: AsyncSession, campaign: Campaign) -> ModelConfig | None:
    """The newest good read of the campaign's model config."""
    if not campaign.model_path:
        return None
    return (
        await db.execute(
            select(ModelConfig)
            .where(ModelConfig.model_path == campaign.model_path, ModelConfig.status == "ok")
            .order_by(ModelConfig.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _vocabulary(db: AsyncSession, campaign: Campaign) -> vocab_mod.Vocabulary | None:
    """The newest usable vocabulary dumped from the campaign's image."""
    row = (
        await db.execute(
            select(EngineVocabulary)
            .where(
                EngineVocabulary.image == campaign.image,
                EngineVocabulary.engine == campaign.engine,
                EngineVocabulary.status != "failed",
            )
            .order_by(EngineVocabulary.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return vocab_mod.from_row(row) if row is not None else None


def _env_vocabulary_problems(
    canonical: dict[str, Any], campaign: Campaign, vocab: vocab_mod.Vocabulary | None
) -> list[ConfigProblem]:
    """Env keys the image does not read; with no env vocabulary for the image,
    every env key unless the campaign lets them through unchecked."""
    if not any(is_env_key(k) for k in canonical):
        return []
    if vocab is not None and vocab.names("env"):
        return [ConfigProblem(**p) for p in vocab_mod.unknown_env_problems(canonical, vocab)]
    if settings_of(campaign).env_without_vocabulary == "warn":
        return []
    return [
        ConfigProblem(
            kind="unknown_env", param=k,
            message=f"{k}: the platform has no list of the environment variables "
            f"{campaign.image} reads yet, so it cannot tell whether the engine would "
            "ignore this one",
        )
        for k in sorted(canonical) if is_env_key(k)
    ]


def _anchor(campaign: Campaign) -> Recipe:
    """What every launch starts from: the campaign's recipe (or the one its
    fields make) with the space's base applied."""
    return recipe_for(campaign, (campaign.search_space or {}).get("base") or {})


def _launches_recipes(machine: Machine | None) -> bool:
    """Whether this machine's runs are LLMServices, which carry the whole recipe."""
    if machine is None or (machine.driver or "") != "k8s":
        return False
    try:
        return resolve_cluster(machine.cluster_id).k8s_workload_kind == "llmservice"
    except Exception:
        return False


def _split_addresses(config: dict[str, Any]) -> tuple[dict[str, Any], list[ConfigProblem]]:
    """The bare flags and env keys (what every check before 1.6 reads), and a
    problem for each address that cannot be read. Role flags and chart values
    are checked on the recipe they produce."""
    flat: dict[str, Any] = {}
    problems: list[ConfigProblem] = []
    for key, value in config.items():
        try:
            kind = parse_address(key).kind
        except BadAddress as exc:
            problems.append(ConfigProblem(kind="bad_key", param=key, message=str(exc)))
            continue
        if kind in ("flag", "env"):
            flat[key] = value
    return flat, problems


def _config_problems(
    config: dict[str, Any],
    campaign: Campaign,
    machine: Machine | None,
    vocab: vocab_mod.Vocabulary | None = None,
) -> tuple[dict[str, Any], list[ConfigProblem]]:
    """The canonical config, and every reason a launch of it would be refused.

    Shared by the refusals (`_validated`) and the dry run (`POST
    /configs/check`), so the two can never disagree about a config.
    """
    canonical_all = _canonical(config, campaign)
    _, address_problems = _split_addresses(canonical_all)
    problems = list(address_problems)
    problems += [
        ConfigProblem(**found)
        for found in recipe_config_problems(
            {k: v for k, v in canonical_all.items() if "." in k and not k.startswith("env.")}
        )
    ]
    flat_config, _ = _split_addresses(config)
    config = flat_config
    _, recipe_only = split_flat(canonical_all)
    if recipe_only and not _launches_recipes(machine):
        problems += [
            ConfigProblem(
                kind="substrate", param=key,
                message=f"{key}: this machine's launch driver applies flags and env only; "
                "pd role flags and chart values need a cluster that launches LLMServices",
            )
            for key in sorted(recipe_only)
        ]
    problems += [
        ConfigProblem(
            kind="placement", param=key,
            message=f"{key} is placement, which the platform sets: remove it",
        )
        for key in sorted(set(config) & set(PLACEMENT_FLAGS))
    ]
    canonical = canonical_all
    for hit in forbidden_hits(
        canonical, campaign.search_space or {}, settings_of(campaign).forbidden(),
        campaign.engine,
        anchor=recipe_flags(base_recipe(campaign)) if campaign.recipe else None,
    ):
        problems.append(ConfigProblem(
            kind="forbidden", param=hit["param"],
            message=f"{hit['param']} is forbidden for this campaign (matches "
            f"{hit['pattern']!r}): leave it at the campaign's value",
        ))
    flat, _ = _split_addresses(canonical)
    args, _env = split_env(flat)
    flags = {k: v for k, v in args.items() if k not in PLACEMENT_FLAGS}
    round_trip = round_trip_problems(flags, campaign.engine)
    problems += [ConfigProblem(**found) for found in round_trip]
    if vocab is not None and not round_trip:
        problems += [
            ConfigProblem(**found)
            for found in vocab_mod.unknown_flag_problems(flags, vocab, campaign.engine)
        ]
    for found in env_problems(flat):
        problems.append(ConfigProblem(**found))
    if not any(p.param.startswith("env.") for p in problems):
        problems += _env_vocabulary_problems(flat, campaign, vocab)
    error = validate_config(
        args,
        ValidationContext(
            gpu_count=(machine.gpu_count if machine else 0) or 0, engine=campaign.engine
        ),
    )
    if error:
        problems.append(ConfigProblem(kind="safety", message=error))
    # A recipe campaign's launch is the recipe with this config applied: what
    # the chart would refuse, it refuses here.
    if campaign.recipe and not any(p.kind == "bad_key" for p in problems):
        problems += [
            ConfigProblem(**found)
            for found in recipe_problems(recipe_for(campaign, canonical))
        ]
    return canonical, problems


def _validated(
    config: dict[str, Any],
    campaign: Campaign,
    machine: Machine | None,
    vocab: vocab_mod.Vocabulary | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Canonical config + deviations, or a 422 saying everything that is wrong
    with it: placement, a forbidden parameter, a value that cannot round-trip
    through a command line, or a safety failure."""
    canonical, problems = _config_problems(config, campaign, machine, vocab)
    if problems:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "; ".join(p.message for p in problems)
        )
    return canonical, space_deviations(canonical, campaign.search_space or {})


async def _validated_or_audit(
    db: AsyncSession,
    caller: PolicyCaller,
    config: dict[str, Any],
    campaign: Campaign,
    machine: Machine | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """`_validated`, with a refusal kept in the campaign's activity log: a
    refused config never becomes a trial, so without this the only trace of
    what a policy tried and was told no is the policy's own log."""
    try:
        return _validated(config, campaign, machine, await _vocabulary(db, campaign))
    except HTTPException as exc:
        _audit(
            db, caller, "policy_config_refused",
            payload={"config": config, "reason": str(exc.detail)[:1000]},
        )
        await db.commit()
        raise


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


def _check_fits_a_node(machine: Machine | None, per_pod: int) -> None:
    """On a node pool each pod's cards are all on one node, so a pod wider
    than the widest node could never be scheduled: refuse it now, rather than
    let it wait for room that will never come. A gang or a pd recipe spreads
    over several pods, so its widest pod is what must fit, not its total.
    Unknown sizes pass (a pool read from a namespace quota has no node sizes)."""
    if machine is None or not is_node_pool(machine) or not machine.node_gpu_max:
        return
    if per_pod > machine.node_gpu_max:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"a pod of this config uses {per_pod} card(s), but the largest node in "
            f"{machine.name} has {machine.node_gpu_max}: no node could ever hold it",
        )


def _free_cards(session: PolicySession, live: list[Run], needed: int) -> list[int]:
    """The first `needed` cards of the allocation no live run of the session
    holds; 429 (wait, as for busy cards) when fewer are free, 422 when the
    allocation could never hold them."""
    allowed = list(session.gpu_indices or [])
    if needed > len(allowed):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"the recipe uses {needed} card(s) but this session holds {len(allowed)}",
        )
    busy = {i for r in live for i in (r.gpu_indices or [])}
    free = [i for i in allowed if i not in busy]
    if len(free) < needed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"the recipe uses {needed} card(s) and {len(free)} are free of your own runs "
            "— release something or wait",
        )
    return free[:needed]


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


def _launch_out(
    run: Run, candidate: Candidate | None = None, waiting_reason: str = ""
) -> LaunchOut:
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
    # Started, but the cluster has no room for its pod yet: queued again, not
    # launching, so a policy can tell waiting for cards from loading a model.
    waiting = run.waiting_since is not None and state == "launching"
    if waiting:
        state = "queued"
    return LaunchOut(
        id=run.id,
        status=state,
        endpoint_url=run.endpoint_url or "",
        launch_command=run.launch_command or "",
        failure_class=run.failure_class or "",
        error=(run.error or "")[:2000],
        config=(candidate.config if candidate else {}),
        deviations=list((candidate.deviations if candidate else None) or []),
        waiting_reason=waiting_reason if waiting else "",
        waiting_since=run.waiting_since if waiting else None,
    )


async def _waiting_reason(db: AsyncSession, run: Run) -> str:
    """What the cluster last said about a run it has not placed."""
    if run.waiting_since is None:
        return ""
    payload = (
        await db.execute(
            select(Event.payload)
            .where(Event.run_id == run.id, Event.kind == "run_waiting_for_cluster")
            .order_by(Event.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return str((payload or {}).get("reason") or "")


def _measures_out(result: Result, candidate: Candidate | None) -> list[ScenarioMeasures]:
    reader = reader_for(result.metrics, result.raw, candidate.config if candidate else None)
    return [ScenarioMeasures(**s) for s in scenarios_out(reader.scenarios)]


# What a benchmark sends, per slug: the benchmark platform is asked once per
# few minutes, not on every manifest read, and a manifest waits for it only
# briefly (a late answer still fills the cache for the next read).
_WORKLOAD_TTL_S = 300.0
_WORKLOAD_WAIT_S = 5.0
_workloads: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _screen_workload(slug: str) -> list[dict[str, Any]]:
    """Best effort: an unreachable benchmark platform leaves it empty."""
    import time

    if not slug:
        return []
    cached = _workloads.get(slug)
    if cached and time.monotonic() - cached[0] < _WORKLOAD_TTL_S:
        return cached[1]
    try:
        from app.evaluation.llmbench import LLMBenchClient

        # One try: a manifest read must not ride out a benchmark platform outage.
        benchmark = next(
            (b for b in LLMBenchClient(max_attempts=1).list_benchmarks() if b.get("slug") == slug),
            None,
        )
    except Exception:
        logger.warning("could not describe benchmark %s for a policy manifest", slug)
        # Remembered too, so an unreachable platform is not asked on every read.
        _workloads[slug] = (time.monotonic(), [])
        return []
    settings = get_settings()
    out = describe_workload(
        (benchmark or {}).get("modules") or [],
        aliases=dict(settings.llmbench_module_families or {}),
        replay_names=(settings.llmbench_replay_module,),
    )
    _workloads[slug] = (time.monotonic(), out)
    return out


async def _screen_workload_soon(slug: str) -> list[dict[str, Any]]:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_screen_workload, slug), timeout=_WORKLOAD_WAIT_S
        )
    except TimeoutError:
        return []


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
    measures: list[ScenarioMeasures] = []
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
        try:
            measures = _measures_out(result, candidate)
        except Exception:
            logger.exception("could not read benchmark %s as measures", run.id)
    from app.staging import stage_of

    return BenchmarkOut(
        id=run.id,
        status=mapping.get(run.status, run.status),
        suite=VERIFY if candidate is not None and stage_of(candidate) == VERIFY else SCREEN,
        summary=summary,
        metrics=metrics,
        measures=measures,
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

    # Production as the platform launches it: the Baselines record over the
    # campaign's anchor, or the recipe as written when none is recorded.
    card_type = machine.gpu_type if machine is not None else ""
    found = await db.run_sync(lambda sync: production_for(sync, campaign, card_type or ""))
    production = ManifestProduction(
        engine_args=dict(found.launch.engine_args),
        env=dict(found.launch.extra_env),
        image=found.launch.image,
        config=dict(found.config),
        source=found.source,
    )

    screen_slug = campaign.benchmark_slug or settings.llmbench_benchmark_slug
    model_row = await _model_config(db, campaign)
    return SessionManifest(
        contract={"version": CONTRACT_VERSION},
        session_id=session.id,
        campaign_id=campaign.id,
        policy={
            "name": policy.name if policy else "",
            "image": policy.image if policy else "",
            "version": policy.version if policy else "",
            "params": dict(knobs.params),
        },
        model=ManifestModel(
            engine=campaign.engine,
            image=campaign.image,
            model_path=campaign.model_path,
            served_model_name=campaign.served_model_name,
            extra_env=campaign.extra_env or {},
            extra_volumes=campaign.extra_volumes or {},
            facts=dict((model_row.facts if model_row else None) or {}),
            recipe=_anchor(campaign).model_dump(),
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
            pool=bool(machine and is_node_pool(machine)),
            node_count=(machine.node_count if machine else 0) or 0,
            node_gpu_max=(machine.node_gpu_max if machine else 0) or 0,
        ),
        objective={SCREEN: _objective(SCREEN), VERIFY: _objective(VERIFY)},
        search_space=campaign.search_space or {},
        space_policy={"deviations": "allowed_if_declared", "forbidden": knobs.forbidden()},
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
            approx_minutes_each=contender_parts(campaign)[0],
        ),
        services=ManifestServices(
            launch=True,
            benchmarks={
                SCREEN: BenchmarkSuite(
                    slug=screen_slug,
                    approx_minutes=_SCREEN_APPROX_MINUTES,
                    dataset_build_id=campaign.dataset_build_id or "",
                    workload=await _screen_workload_soon(screen_slug),
                )
            },
            state=True,
            diagnostics=True,
            env_keys=True,
            vocabulary=True,
            recipes=True,
            production_config=True,
            history=True,
        ),
        prior=prior,
        measures=measure_catalogue(),
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

    canonical, dev = await _validated_or_audit(
        db, caller, dict(body.engine_args or {}), campaign, machine
    )
    needed = config_cards(campaign, canonical)
    live = await _live_session_runs(db, session)
    gpu_indices = list(body.gpu_indices)
    # A machine with no cards (a CPU-only slice: kind, the demo's mock
    # engine) gives the session none, and its launches ask for none whatever
    # the config's parallelism, the way a plain campaign runs there. Asking
    # for a card it does not hold is still refused, as outside the allocation.
    if session.gpu_indices and needed != len(gpu_indices):
        if base_recipe(campaign) is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"config uses {needed} card(s) (tp×dp×pp) but {len(gpu_indices)} were "
                "requested — they must match",
            )
        # Under a recipe its shape decides the cards (lws.size, pd roles,
        # parallelism set in the recipe rather than the config), which a
        # policy counting the config's own tp×dp×pp cannot see: the platform
        # takes them from the allocation instead.
        gpu_indices = _free_cards(session, live, needed)
    _check_fits_a_node(machine, config_pod_cards(campaign, canonical))
    port = _check_cards_and_port(session, live, gpu_indices, body.port)

    candidate = await _make_candidate(db, campaign, canonical, dev)
    run = Run(
        campaign_id=campaign.id,
        candidate_id=candidate.id,
        machine_id=session.machine_id,
        kind=RunKind.POLICY_LAUNCH.value,
        status=RunStatus.PENDING.value,
        gpu_indices=gpu_indices,
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
        payload={"config": canonical, "gpus": gpu_indices, "port": port},
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
    return _launch_out(
        run, await db.get(Candidate, run.candidate_id), await _waiting_reason(db, run)
    )


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
        launch_run_id=parent.id,
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
    if session.gpu_indices and not body.gpu_indices:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "gpu_indices is empty: name the cards the engine you serve runs on",
        )
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
    canonical, dev = await _validated_or_audit(db, caller, body.engine_args, campaign, machine)
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


# -- diagnostics (1.3) -----------------------------------------------------------------


@router.post("/configs/check", response_model=ConfigCheckOut)
async def check_config(
    body: ConfigCheckIn,
    caller: PolicyCaller = Depends(get_policy_session),
    db: AsyncSession = Depends(get_async_session),
):
    """Everything a launch of this config would meet — refusals, the canonical
    echo, the rendered command, catalog warnings, deviations — without
    spending a launch. Never 422s: the answer is the point."""
    _session, campaign, machine = await _context(db, caller)
    vocab = await _vocabulary(db, campaign)
    canonical, problems = _config_problems(
        dict(body.engine_args or {}), campaign, machine, vocab
    )
    # With the image's own parser at hand an unknown flag is a refusal above;
    # the platform's catalog is only the fallback guess.
    known = set() if vocab is not None and vocab.names("flag") else known_flags(campaign.engine)
    flat, _ = _split_addresses(canonical)
    args, env = split_env(flat)
    normalized, _ = normalize_args(campaign.engine, args)
    # Only what the policy chose: a key the campaign itself sets to this value
    # (the base config) is the operator's, and repeating its warning on every
    # check buries the ones about the policy's own flags.
    base, _ = normalize_args(campaign.engine, (campaign.search_space or {}).get("base") or {})
    warnings = [
        f"{key} is not in the platform's {campaign.engine} flag catalog — the engine "
        "may still accept it; a typo fails only at launch"
        for key in sorted(normalized)
        if known and key not in known and not (key in base and base[key] == normalized[key])
    ]
    command: list[str] = []
    recipe_out: dict[str, Any] = {}
    shape_out: dict[str, Any] = {}
    commands: dict[str, list[str]] = {}
    if not any(p.kind in ("not_renderable", "bad_key") for p in problems):
        command = render_args(args, campaign.engine)
        recipe = recipe_for(campaign, canonical)
        recipe_out = recipe.model_dump()
        shape_out = recipe_shape(recipe).payload()
        commands = chart_commands(recipe)
    return ConfigCheckOut(
        ok=not problems,
        config=canonical,
        cards=config_cards(campaign, canonical),
        command=command,
        env=env,
        problems=problems,
        warnings=warnings,
        deviations=space_deviations(canonical, campaign.search_space or {}),
        recipe=recipe_out,
        shape=shape_out,
        commands=commands,
    )


@router.get("/engine/vocabulary", response_model=VocabularyOut)
async def engine_vocabulary(
    kind: str = Query("", pattern="^(|flag|env)$"),
    query: str = Query("", max_length=200),
    level: str = Query("", pattern="^(|high|mid|low|unknown)$"),
    limit: int = Query(100, ge=1, le=1000),
    caller: PolicyCaller = Depends(get_policy_session),
    db: AsyncSession = Depends(get_async_session),
):
    """What the campaign's engine image reads, filtered: `kind`, a substring
    of the name or help (`query`), and the lowest `level` wanted ("mid" =
    high and mid)."""
    _session, campaign, machine = await _context(db, caller)
    row = (
        await db.execute(
            select(EngineVocabulary)
            .where(
                EngineVocabulary.image == campaign.image,
                EngineVocabulary.engine == campaign.engine,
            )
            .order_by(EngineVocabulary.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    model_row = await _model_config(db, campaign)
    model = {
        "model_facts": dict(model_row.facts or {}) if model_row else {},
        "model_config_json": dict(model_row.config or {}) if model_row else {},
    }
    if row is None or row.status == "failed":
        return VocabularyOut(
            status="none", engine=campaign.engine, image=campaign.image,
            notes=list(row.notes or []) if row is not None else [], **model,
        )
    rows = vocab_mod.lookup(
        vocab_mod.annotate(vocab_mod.from_row(row), (machine.gpu_type if machine else "") or ""),
        kind=kind, query=query, level=level,
    )
    return VocabularyOut(
        status=row.status, engine=row.engine, engine_version=row.engine_version or "",
        image=row.image, total=len(rows), notes=list(row.notes or []), entries=rows[:limit],
        **model,
    )


def _filter_log(text: str, grep: str | None, context: int, tail: int) -> str:
    """The last `tail` lines of a log, or of the lines matching `grep` (a
    case-insensitive regex, or a plain substring when it is not one) with
    `context` lines either side."""
    lines = text.splitlines()
    if grep:
        try:
            pattern = re.compile(grep, re.IGNORECASE)
            hit = pattern.search
        except re.error:
            needle = grep.lower()

            def hit(line: str) -> bool:
                return needle in line.lower()

        keep: set[int] = set()
        for index, line in enumerate(lines):
            if hit(line):
                keep.update(range(max(0, index - context), min(len(lines), index + context + 1)))
        picked: list[str] = []
        previous = -2
        for index in sorted(keep):
            if index != previous + 1 and picked:
                picked.append("--")
            picked.append(lines[index])
            previous = index
        lines = picked
    return "\n".join(lines[-tail:])


@router.get("/launches/{run_id}/log", response_class=PlainTextResponse)
async def launch_log(
    run_id: int,
    tail: int = Query(default=500, ge=1, le=20000),
    grep: str | None = Query(default=None, max_length=200),
    context: int = Query(default=0, ge=0, le=50),
    caller: PolicyCaller = Depends(get_policy_session),
    db: AsyncSession = Depends(get_async_session),
):
    """The engine's log: live from its container while it runs, the saved copy
    after. Startup errors, OOM traces and the engine's own throughput lines
    are here — the evidence a failed or slow launch leaves."""
    run = await _session_run(db, caller, run_id, RunKind.POLICY_LAUNCH.value)
    return _filter_log(await read_run_log(db, run), grep, context, tail)


@router.get("/launches/{run_id}/environment", response_model=EnvironmentPart)
async def launch_environment(
    run_id: int,
    caller: PolicyCaller = Depends(get_policy_session),
    db: AsyncSession = Depends(get_async_session),
):
    """What the launch actually ran on: engine, torch and CUDA versions, image
    digest, card. Recorded once the engine is up; fill a contender's
    `launch_spec.environment` from it to make its verdict promotable."""
    run = await _session_run(db, caller, run_id, RunKind.POLICY_LAUNCH.value)
    bundle = await load_run(db, run.id)
    return environment_part(bundle)


@router.get("/benchmarks/{run_id}/results", response_model=ResultsPart)
async def benchmark_results(
    run_id: int,
    caller: PolicyCaller = Depends(get_policy_session),
    db: AsyncSession = Depends(get_async_session),
):
    """The benchmark in full: every scenario, every concurrency level, latency
    percentiles, the best level that met the gate. `status` says whether it is
    measured yet."""
    run = await _session_run(db, caller, run_id, RunKind.EXTERNAL.value)
    bundle = await load_run(db, run.id)
    return results_part(bundle)


@router.get("/benchmarks/{run_id}/results/raw")
async def benchmark_results_raw(
    run_id: int,
    caller: PolicyCaller = Depends(get_policy_session),
    db: AsyncSession = Depends(get_async_session),
):
    """The benchmark platform's own result payload, exactly as harvested."""
    run = await _session_run(db, caller, run_id, RunKind.EXTERNAL.value)
    bundle = await load_run(db, run.id)
    if bundle is None or bundle.result is None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"benchmark {run_id} has no result yet")
    return bundle.result.raw or {}


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
    vocab = await _vocabulary(db, campaign)
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
            canonical, dev = _validated(
                dict(entry.launch_spec.engine_args or {}),
                campaign, machine, vocab,
            )
        except HTTPException as exc:
            rejected.append({"rank": entry.rank, "reason": str(exc.detail)})
            continue
        spec = entry.launch_spec.model_dump(mode="json")
        spec["engine_args"] = canonical
        # 1.6: what this contender launches and what a publish writes.
        spec["recipe"] = recipe_for(campaign, canonical).model_dump()
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

    # One update at a time per session: two PUTs a moment apart (two trials
    # finishing together) would both read no current list and both insert,
    # and the next PUT would supersede the pair. The second waits here and
    # then reads what the first wrote.
    await db.execute(
        select(PolicySession.id).where(PolicySession.id == session.id).with_for_update()
    )
    current = (
        await db.execute(
            select(PolicyContender).where(
                PolicyContender.session_id == session.id,
                PolicyContender.status == ContenderStatus.REGISTERED.value,
            )
        )
    ).scalars().all()

    # Same contenders (rank + config unchanged): no supersede, no new rows, no
    # event — the ledger keeps to real changes. But the evidence is refreshed
    # in place: a policy re-declares the same best after measuring it again,
    # and a verdict or report built on the first measurement alone would read
    # an outdated number.
    def _sig(rows: list[PolicyContender]) -> list[tuple[int, str]]:
        return sorted((r.rank, r.config_hash) for r in rows)

    if _sig(current) == _sig(accepted_rows):
        fresh = {(r.rank, r.config_hash): r for r in accepted_rows}
        for row in current:
            update = fresh[(row.rank, row.config_hash)]
            row.evidence = update.evidence
            row.trial_ids = update.trial_ids
            row.launch_spec = update.launch_spec
        await db.commit()
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


# -- history (1.4) -----------------------------------------------------------------


@router.put("/session/digest", status_code=status.HTTP_204_NO_CONTENT)
async def put_digest(
    body: DigestIn,
    caller: PolicyCaller = Depends(get_policy_session),
    db: AsyncSession = Depends(get_async_session),
):
    """What tonight learned, for other campaigns. Replaces any earlier digest
    of this session; accepted until the session ends."""
    session = await db.get(PolicySession, caller.session.id)
    if session.status in {s.value for s in TERMINAL_SESSION_STATES}:
        raise HTTPException(status.HTTP_409_CONFLICT, f"the session is {session.status}")
    session.digest = body.model_dump(mode="json")
    session.digest_at = datetime.now(UTC)
    _audit(db, caller, "policy_digest_written", payload={"bytes": len(body.summary)})
    await db.commit()


_HISTORY_MATCHES = ("model", "engine")


@router.get("/history", response_model=list[HistoryEntry])
async def history(
    caller: PolicyCaller = Depends(get_policy_session),
    db: AsyncSession = Depends(get_async_session),
    match: str = Query("model", description="model: same engine and model; engine: same engine"),
    limit: int = Query(10, ge=1, le=50),
):
    """Other campaigns' nights, newest first: what each policy wrote it
    learned, under which conditions, and what the platform validated. Only
    campaigns of the same owner; this campaign's own nights are in /state.
    Findings from another workload or image are hints, not facts here."""
    if match not in _HISTORY_MATCHES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"match must be one of {list(_HISTORY_MATCHES)}"
        )
    _, campaign, _ = await _context(db, caller)
    query = (
        select(PolicySession, Campaign)
        .join(Campaign, PolicySession.campaign_id == Campaign.id)
        .where(
            Campaign.owner_id == campaign.owner_id,
            Campaign.id != campaign.id,
            Campaign.engine == campaign.engine,
            (PolicySession.digest.is_not(None)) | (PolicySession.finalized_at.is_not(None)),
        )
        .order_by(PolicySession.id.desc())
    )
    if match == "model":
        query = query.where(
            (Campaign.served_model_name == campaign.served_model_name)
            | (Campaign.model_path == campaign.model_path)
        )
    out: list[HistoryEntry] = []
    for past, past_campaign in (await db.execute(query.limit(limit * 3))).all():
        entry = await _history_entry(db, past, past_campaign)
        if entry.digest is not None or entry.verdicts:
            out.append(entry)
        if len(out) >= limit:
            break
    return out


async def _history_entry(
    db: AsyncSession, past: PolicySession, campaign: Campaign
) -> HistoryEntry:
    from app.evaluation.ranking import leaderboard_entries

    machine = await db.get(Machine, past.machine_id) if past.machine_id else None
    objective = stage_objective(campaign, SCREEN)
    rows = (
        await db.execute(
            select(Run, Candidate, Result)
            .join(Candidate, Run.candidate_id == Candidate.id)
            .join(Result, Result.run_id == Run.id)
            .where(
                Run.campaign_id == campaign.id,
                Run.status == RunStatus.SUCCEEDED.value,
                Result.source == "llmbench",
            )
        )
    ).all()
    night = {run.id for run, _, _ in rows if run.policy_session_id == past.id}
    entries = [
        e for e in leaderboard_entries(campaign, list(rows))
        if e.stage == VERIFY and e.run_id in night
    ]
    production = [dict(e.config) for e in entries if e.is_baseline]
    verdicts = [
        HistoryVerdict(
            config=dict(e.config),
            is_production=e.is_baseline,
            value=e.score,
            vs_production=None if e.is_baseline else e.vs_baseline,
            holds_redlines=e.holds_redlines,
            same_as_production=not e.is_baseline and dict(e.config) in production,
        )
        for e in entries
    ]
    digest = None
    if past.digest:
        try:
            digest = DigestIn.model_validate(past.digest)
        except ValueError:
            digest = None
    return HistoryEntry(
        campaign_id=campaign.id,
        campaign_name=campaign.name,
        session_id=past.id,
        ended_at=past.finalized_at,
        end_reason=past.search_end_reason or past.status,
        conditions=HistoryConditions(
            engine=campaign.engine,
            image=campaign.image,
            model_path=campaign.model_path,
            served_model_name=campaign.served_model_name,
            gpu_type=machine.gpu_type if machine is not None else "",
            cards=settings_of(campaign).cards,
            benchmark_slug=campaign.benchmark_slug or "",
            objective=ManifestObjective(
                target_metric=target_metric(objective),
                direction=direction(objective),
                redlines=redlines(objective),
            ),
            forbidden=list(settings_of(campaign).forbidden()),
        ),
        verdicts=verdicts,
        digest=digest,
    )


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
