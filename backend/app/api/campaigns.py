import logging
from datetime import UTC, datetime, timedelta

import anyio
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.control.launch import MachineInfo, get_driver
from app.control.launch import preflight as pf
from app.control.orchestrator import lifecycle
from app.control.orchestrator import schedule as sched
from app.control.search.parity import missing_flags
from app.control.search.space import candidate_count, expand, space_errors, swept_keys
from app.control.search.validation import cards_used
from app.core.auth import get_current_user
from app.datasets import pinning
from app.datasets.profiles import DatasetProfileClient, DatasetProfileError
from app.db.base import get_async_session
from app.db.models import (
    TERMINAL_RUN_STATES,
    TERMINAL_SESSION_STATES,
    Campaign,
    CampaignStatus,
    Candidate,
    Event,
    Machine,
    MachineGroup,
    MachineGroupMember,
    Policy,
    PolicySession,
    Result,
    Run,
    User,
    is_baseline_candidate,
    is_in_place_baseline,
)
from app.evaluation.llmbench import LLMBenchClient
from app.evaluation.ranking import leaderboard_entries
from app.metrics_catalog import DEFAULT_VERIFY_TARGET_METRIC
from app.objective import target_metric
from app.reporting import render_campaign_report
from app.schemas.core import (
    CampaignCreate,
    CampaignOut,
    CampaignScheduleUpdate,
    CampaignStatusUpdate,
    CandidateOut,
    DeployBranchIn,
    LeaderboardEntry,
    MachineWarningOut,
    RunOut,
)
from app.schemas.policy import PolicySettings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/campaigns", tags=["campaigns"])

_ALLOWED_ENGINES = {"sglang", "vllm"}
_TERMINAL_RUN_VALUES = [s.value for s in TERMINAL_RUN_STATES]


async def _node_group_errors(session: AsyncSession, body: CampaignCreate) -> list[str]:
    """Validate a declared node group at save time.

    Cheap and static: the group must exist, and vLLM is refused outright — its
    multi-node topology (a Ray head plus workers, or external_launcher) is a
    different bootstrap that the platform does not have, and half-wiring it would
    fail at launch with a config that looks fine.
    """
    name = (body.node_group or "").strip()
    if not name:
        return []
    if body.engine != "sglang":
        return [
            f"{body.engine} multi-node deployment is not supported yet; use sglang "
            "for a node group"
        ]
    group = (
        await session.execute(select(MachineGroup).where(MachineGroup.name == name))
    ).scalar_one_or_none()
    if group is None:
        return [f"no node group named {name!r} — create one on the Resources page"]
    # A gang on the cluster is one LeaderWorkerSet (design step 9). Until the k8s
    # driver renders that, launching N separate Deployments would be N
    # single-node engines rather than the deployment that was asked for, so the
    # pin is refused here instead of silently mislaunching it.
    drivers = (
        await session.execute(
            select(Machine.driver)
            .join(MachineGroupMember, MachineGroupMember.machine_id == Machine.id)
            .where(MachineGroupMember.group_id == group.id)
        )
    ).scalars().all()
    non_ssh = sorted({(driver or "ssh_docker") for driver in drivers} - {"ssh_docker"})
    if non_ssh:
        return [
            f"node group {name!r} runs on {', '.join(non_ssh)}, and multi-node "
            "deployment is only implemented for ssh+docker hosts so far"
        ]
    return []



def _staging_errors(body: CampaignCreate) -> list[str]:
    """Half a staged campaign is worse than none.

    A top-k with no benchmark would re-run the winners against the same cheap
    test and present the result as verification; a benchmark with no top-k is
    configuration that silently never fires. Both read, at 8am, as "the second
    stage did not happen" with nothing to say why.
    """
    errors: list[str] = []
    if body.verify_top_k < 0:
        errors.append("verify_top_k must be >= 0")
    if body.verify_top_k > 0 and not body.verify_benchmark_slug.strip():
        errors.append(
            "verify_top_k needs verify_benchmark_slug — which benchmark should "
            "the best candidates be re-measured against?"
        )
    if body.verify_benchmark_slug.strip() and body.verify_top_k <= 0:
        errors.append(
            "verify_benchmark_slug needs verify_top_k > 0, or nothing is ever "
            "sent to it"
        )
    if body.verify_top_k > 0 and body.verify_max_run_minutes < 1:
        errors.append("verify_max_run_minutes must be >= 1")
    if body.dataset_policy and body.dataset_policy not in pinning.POLICIES:
        errors.append(f"dataset_policy must be one of {list(pinning.POLICIES)}")
    # No longer tied to the verify stage: when the single benchmark IS the
    # replay, the primary stage pins the dataset. A profile set on a campaign
    # whose benchmark never replays is harmless — the pin is computed and simply
    # never matched against — so it is not worth refusing here.
    return errors


@router.get("/dataset-profiles", summary="Replay collection profiles on LLMBench")
async def dataset_profiles(_: User = Depends(get_current_user)):
    """What a campaign may pin, with what each profile currently holds.

    Proxied rather than mirrored: the profiles live on LLMBench, and a copy
    here would be one more thing to be wrong. `managed` marks the ones nothing
    rebuilds on a schedule — the only kind a campaign can hold for its whole
    life, because any other will roll underneath it.
    """
    def _read() -> list[dict]:
        return DatasetProfileClient().list_profiles()

    try:
        profiles = await anyio.to_thread.run_sync(_read)
    except DatasetProfileError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"could not reach the benchmark platform: {exc}"
        ) from exc

    out = []
    for profile in profiles:
        current = profile.get("current") or {}
        out.append(
            {
                "name": profile.get("name", ""),
                "display_name": profile.get("display_name", ""),
                "managed": not (profile.get("schedule_interval_hours") or 0),
                "enabled": bool(profile.get("enabled", True)),
                "build_id": current.get("build_id", ""),
                "records": current.get("records"),
                "built_at": current.get("built_at", ""),
                "window_start": current.get("window_start", ""),
                "window_end": current.get("window_end", ""),
            }
        )
    return out


@router.get("", response_model=list[CampaignOut])
async def list_campaigns(
    _: User = Depends(get_current_user), session: AsyncSession = Depends(get_async_session)
):
    return (
        (await session.execute(select(Campaign).order_by(Campaign.id.desc()))).scalars().all()
    )


@router.post("", response_model=CampaignOut)
async def create_campaign(
    body: CampaignCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    if body.engine not in _ALLOWED_ENGINES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "engine must be sglang|vllm")
    # With no policy the platform simply enumerates the declared space; with
    # one, the container decides what to try and the space is only the bounds
    # it must stay inside.
    if body.policy_id is not None:
        policy = await session.get(Policy, body.policy_id)
        if policy is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, f"no policy with id {body.policy_id}"
            )
        try:
            PolicySettings.model_validate(body.policy_settings or {})
        except ValidationError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, f"policy_settings: {exc.errors()}"
            ) from exc
    if body.confirm_top_k < 0 or body.confirm_repeats < 1:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "confirm_top_k must be >= 0 and confirm_repeats >= 1",
        )
    for message in _staging_errors(body):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, message)
    for message in await _node_group_errors(session, body):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, message)
    # A malformed space does not fail loudly — it quietly expands to a different
    # candidate list than the one that was reviewed. Catch it at creation, not
    # at 2am when the policy has already spent half the night on it.
    errors = space_errors(body.search_space or {})
    if errors:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "; ".join(errors))
    # A schedule that does not parse leaves the campaign asleep forever, and
    # the only symptom is nothing happening at 23:00 — the worst possible time
    # to discover a typo.
    schedule_errors = sched.errors(body.daily_start, body.daily_end, body.schedule_timezone)
    if schedule_errors:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "; ".join(schedule_errors))
    campaign = Campaign(owner_id=user.id, **body.model_dump())
    # A campaign that carries a clock starts under it, not in draft: leaving it
    # DRAFT would mean someone still has to press Start, which is exactly the
    # 23:00 keyboard visit the schedule exists to remove.
    if sched.from_campaign(campaign) is not None:
        campaign.status = CampaignStatus.SCHEDULED.value
    session.add(campaign)
    await session.flush()
    session.add(
        Event(
            actor=user.username,
            kind="campaign_created",
            campaign_id=campaign.id,
            payload={"name": campaign.name},
        )
    )
    await session.commit()
    await session.refresh(campaign)
    return campaign


@router.get("/{campaign_id}", response_model=CampaignOut)
async def get_campaign(
    campaign_id: int,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such campaign")
    return await _campaign_out(session, campaign)


_LIVE_CAMPAIGN_STATES = (
    CampaignStatus.DRAFT.value,
    CampaignStatus.SCHEDULED.value,
    CampaignStatus.ACTIVE.value,
    CampaignStatus.PAUSED.value,
)


async def _campaign_out(session: AsyncSession, campaign: Campaign) -> CampaignOut:
    """CampaignOut plus the row-only lease findings about its pinned machines.
    Only while the campaign can still run — a finished one's pool is history."""
    out = CampaignOut.model_validate(campaign)
    if campaign.status in _LIVE_CAMPAIGN_STATES:
        machines = (await session.execute(select(Machine))).scalars().all()
        out.machine_warnings = [
            MachineWarningOut(**w)
            for w in lifecycle.machine_warnings(
                machines, campaign.machine_names or [],
                finish_by=lifecycle.as_utc(campaign.window_end),
            )
        ]
    return out


@router.put("/{campaign_id}/deploy-branch", response_model=CampaignOut)
async def set_deploy_branch(
    campaign_id: int,
    body: DeployBranchIn,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """Where this campaign's winner is proposed — and whether it proposes
    itself.

    The repo keeps one branch per (model x card x engine), so a bound baseline
    can face several of them and the binding's own branch is only a default.
    Empty clears it, back to that default. Not validated against the repo here
    — the branch list comes from GitLab in the UI, and a branch that has since
    disappeared is reported when the merge request is previewed, which is the
    moment it matters.
    """
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such campaign")
    campaign.deploy_branch = (body.branch or "").strip()
    if body.auto_promote is not None:
        campaign.auto_promote = body.auto_promote
    session.add(
        Event(
            actor=user.username,
            kind="campaign_deploy_branch_set",
            campaign_id=campaign.id,
            payload={"branch": campaign.deploy_branch, "auto_promote": campaign.auto_promote},
        )
    )
    await session.commit()
    await session.refresh(campaign)
    return await _campaign_out(session, campaign)


@router.put("/{campaign_id}/status", response_model=CampaignOut)
async def set_campaign_status(
    campaign_id: int,
    body: CampaignStatusUpdate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    allowed = (
        CampaignStatus.ACTIVE.value,
        CampaignStatus.PAUSED.value,
        CampaignStatus.SCHEDULED.value,
    )
    if body.status not in allowed:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"status must be one of {list(allowed)}"
        )
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such campaign")
    if body.status == CampaignStatus.SCHEDULED.value and not sched.from_campaign(campaign):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "this campaign has no nightly schedule to hand control back to",
        )
    if (
        body.status == CampaignStatus.ACTIVE.value
        and campaign.policy_id
        and campaign.window_end is None
        and not sched.from_campaign(campaign)
    ):
        # A policy session's deadlines are computed from the window; with no
        # window there is no search_deadline, no validation reserve, and no
        # hard cutoff — a container that would run until someone remembers it.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "a policy campaign needs a window: set a schedule, a one-off "
            "window_start/window_end, or use Force start (which writes one)",
        )
    campaign.status = body.status
    session.add(
        Event(
            actor=user.username,
            kind="campaign_status_changed",
            campaign_id=campaign.id,
            payload={"status": body.status},
        )
    )
    await session.commit()
    await session.refresh(campaign)
    return campaign


class PreflightRequest(BaseModel):
    """A campaign as it stands, saved or not.

    Takes a draft rather than an id so the wizard can check before anything is
    written — the whole value is finding these out before a night is committed
    to them, not after.
    """

    engine: str = "sglang"
    image: str = ""
    model_path: str = ""
    served_model_name: str = ""
    machine_names: list[str] = Field(default_factory=list)
    # A node group each run would deploy across, by name. Declared here so the
    # wizard can check the pin before anything is written — the machines it
    # resolves to are the ones probed below.
    node_group: str = ""
    service_port: int = 28200
    extra_volumes: dict[str, str] = Field(default_factory=dict)
    search_space: dict = Field(default_factory=dict)
    # The two benchmarks and the two objectives, so the check can say whether
    # each one can actually report what its objective ranks on.
    benchmark_slug: str = ""
    objective: dict = Field(default_factory=dict)
    verify_benchmark_slug: str = ""
    verify_objective: dict = Field(default_factory=dict)
    dataset_profile: str = ""


def _benchmark_catalog() -> dict[str, list[str]] | None:
    """What LLMBench offers this account. None when it could not be asked —
    which is a fact about the platform right now, never a verdict on a
    campaign, so the checks built from it degrade to SKIP."""
    try:
        return LLMBenchClient().modules_by_benchmark()
    except Exception as exc:  # unreachable, unauthorized, shape changed
        logger.info("preflight could not list benchmarks: %s", exc)
        return None


def _dataset_wiring() -> tuple[dict[str, str] | None, list[str] | None]:
    """Which profile each benchmark replays, and which profiles exist.

    Both None when LLMBench could not be asked — same rule as the benchmark
    catalog: a fact about the platform right now is never a verdict on a
    campaign, so the check degrades to SKIP rather than blocking a night.
    """
    client = LLMBenchClient()
    try:
        wired = client.dataset_profiles_by_benchmark()
    except Exception as exc:
        logger.info("preflight could not read benchmark datasets: %s", exc)
        return None, None
    try:
        profiles = DatasetProfileClient(client).names()
    except Exception as exc:
        # The wiring check still works without the list; only the "does this
        # profile exist" half is lost.
        logger.info("preflight could not list dataset profiles: %s", exc)
        profiles = None
    return wired, profiles


def _benchmark_checks(body: PreflightRequest, catalog: dict[str, list[str]] | None) -> list:
    checks = [
        pf.benchmark_check(
            "benchmark", "Benchmark", body.benchmark_slug, catalog,
            target_metric(body.objective),
        )
    ]
    if body.verify_benchmark_slug:
        checks.append(
            pf.benchmark_check(
                "verify_benchmark", "Replay benchmark", body.verify_benchmark_slug, catalog,
                target_metric(
                    body.verify_objective or {"target_metric": DEFAULT_VERIFY_TARGET_METRIC}
                ),
            )
        )
        wired, profiles = _dataset_wiring()
        checks.append(
            pf.dataset_check(
                body.dataset_profile.strip(), body.verify_benchmark_slug, wired, profiles
            )
        )
    return checks


def _widest_candidate(search_space: dict) -> int:
    """Cards the largest configuration in this space would occupy."""
    try:
        return max((cards_used(c) for c in expand(search_space or {})), default=0)
    except Exception:
        return 0


def _run_preflight(body: PreflightRequest, machines: list[Machine]) -> list[dict]:
    """The machine-side probes, once per candidate machine.

    Runs in a worker thread: it is ssh, and a form that blocks the event loop
    for two seconds per machine is a form nobody waits for.
    """
    widest = _widest_candidate(body.search_space)
    space_check = pf.space_check(
        candidate_count(body.search_space or {}), space_errors(body.search_space or {})
    )
    # One HTTP call, not one per machine: the answer is about the campaign.
    benchmark_checks = _benchmark_checks(body, _benchmark_catalog())
    # A declared node group is a fact about the deployment, worth one line so the
    # author sees the shape they are about to save (and that the Machines list is
    # no longer the pin). Its existence was checked when the campaign was built;
    # the async handler resolves it again so a group deleted since is a note.
    multi_node_check = (
        [
            pf.Check(
                "multi_node", "Multi-node deployment", pf.PASS,
                f"each run deploys across node group {body.node_group!r} "
                "(the Machines list is ignored)",
            )
        ]
        if body.node_group
        else []
    )

    out: list[dict] = []
    for machine in machines:
        info = MachineInfo.of(machine)
        # Each machine is probed through its own substrate. The ssh probe reaches
        # into `driver._ssh`, which only the ssh_docker driver has; a k8s machine
        # is scheduled by the cluster, so its readiness is a cluster-side concern
        # this form cannot answer from here. Say so rather than crash or pretend.
        driver = get_driver(machine.driver or "ssh_docker")
        if not hasattr(driver, "_ssh"):
            checks = [pf.Check(
                "substrate", "Deployment substrate", pf.SKIP,
                f"{machine.name} runs on the {driver.name} substrate; "
                "placement and readiness are decided cluster-side",
                "Machine-side preflight (ssh, ports, GPUs) only applies to bare-metal "
                "hosts. The k8s driver validates against the cluster at launch time.",
            )]
        else:
            checks = pf.inspect(
                driver, info,
                image=body.image, model_path=body.model_path,
                volumes=body.extra_volumes, port=body.service_port,
                widest_candidate_cards=widest,
            )
        # Parity is not a machine probe but it is the same kind of finding —
        # cheap to learn, expensive to discover at 3am — so it belongs in the
        # same list rather than in a banner people close.
        base = (body.search_space or {}).get("base") or {}
        config = {**base, **dict.fromkeys(swept_keys(body.search_space or {}))}
        missing = missing_flags(config, machine.baseline, body.served_model_name)
        container = missing[0]["container"] if missing else ""
        checks.append(pf.parity_check(missing, container))
        checks.append(space_check)
        checks.extend(benchmark_checks)
        checks.extend(multi_node_check)
        out.append(pf.summarize(machine.name, checks))
    return out


@router.post(
    "/preflight",
    summary="Check a campaign before committing a night to it",
    description="Cheap checks for expensive failures: is the machine reachable, is the "
    "image there, does the model path exist, is the port free and actually routable, do "
    "the bind mounts exist, does the widest candidate fit, and does the config match the "
    "production service it will be compared against. Takes a draft, so the wizard can ask "
    "before saving anything.",
)
async def preflight(
    body: PreflightRequest,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    machines = (await session.execute(select(Machine).order_by(Machine.id))).scalars().all()
    # A declared node group IS the pin, exactly as it is for the campaign the
    # wizard is about to save. Without this the group's machines would fall
    # through to "any machine" and the preflight would probe the whole fleet.
    allowed: list[str] = []
    group = (
        await session.execute(
            select(MachineGroup).where(MachineGroup.name == body.node_group)
        )
    ).scalar_one_or_none() if body.node_group else None
    if body.node_group and group is None:
        return {
            "machines": [],
            "ok": False,
            "note": f"no node group named {body.node_group!r} — it may have been dissolved",
        }
    if group is not None:
        members = (
            await session.execute(
                select(Machine)
                .join(MachineGroupMember, MachineGroupMember.machine_id == Machine.id)
                .where(MachineGroupMember.group_id == group.id)
            )
        ).scalars().all()
        allowed = [machine.name for machine in members]
        names = set(allowed)
        candidates = [machine for machine in machines if machine.name in names]
    else:
        allowed = body.machine_names or []
        candidates = [m for m in machines if not allowed or m.name in allowed]
    if not candidates:
        return {
            "machines": [],
            "ok": False,
            "note": (
                f"pinned to {', '.join(allowed)}, which is not registered"
                if allowed else "no machines are registered"
            ),
        }
    results = await anyio.to_thread.run_sync(_run_preflight, body, candidates)
    return {"machines": results, "ok": all(r["ok"] for r in results), "note": ""}


@router.get(
    "/{campaign_id}/preflight",
    summary="Check a saved campaign against its machines",
    description="The same checks as `POST /campaigns/preflight`, for a campaign that "
    "already exists — worth re-running before a start, since the machine may have "
    "changed since it was created.",
)
async def campaign_preflight(
    campaign_id: int,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such campaign")
    body = PreflightRequest(
        engine=campaign.engine,
        image=campaign.image,
        model_path=campaign.model_path,
        served_model_name=campaign.served_model_name,
        machine_names=campaign.machine_names or [],
        node_group=campaign.node_group or "",
        service_port=campaign.service_port,
        extra_volumes=campaign.extra_volumes or {},
        search_space=campaign.search_space or {},
        benchmark_slug=campaign.benchmark_slug or "",
        objective=campaign.objective or {},
        verify_benchmark_slug=campaign.verify_benchmark_slug or "",
        verify_objective=campaign.verify_objective or {},
        dataset_profile=campaign.dataset_profile or "",
    )
    return await preflight(body, _, session)


@router.get(
    "/{campaign_id}/schedule",
    summary="When this campaign is awake",
    description="The nightly window as declared, plus the occurrence running now and "
    "the next one due — so a reader never has to do the midnight arithmetic themselves.",
)
async def get_schedule(
    campaign_id: int,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such campaign")
    schedule = sched.from_campaign(campaign)
    moment = sched.now_utc()
    current = sched.current_window(moment, schedule)
    upcoming = sched.next_window(moment, schedule)
    return {
        "daily_start": campaign.daily_start,
        "daily_end": campaign.daily_end,
        "timezone": campaign.schedule_timezone or sched.DEFAULT_TIMEZONE,
        "schedule_until": campaign.schedule_until,
        "summary": sched.describe(schedule),
        "overnight": bool(schedule and schedule.crosses_midnight),
        "window_start": campaign.window_start,
        "window_end": campaign.window_end,
        "current_window": (
            {"start": current[0], "end": current[1]} if current else None
        ),
        "next_window": (
            {"start": upcoming[0], "end": upcoming[1]} if upcoming else None
        ),
        "finished": sched.is_finished(moment, schedule) if schedule else False,
    }


@router.put(
    "/{campaign_id}/schedule",
    response_model=CampaignOut,
    summary="Set or clear the nightly window",
    description="Takes effect on the next worker tick, tonight included. Clearing both "
    "times removes the schedule and leaves the campaign under manual control.",
)
async def set_schedule(
    campaign_id: int,
    body: CampaignScheduleUpdate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such campaign")
    errors = sched.errors(body.daily_start, body.daily_end, body.schedule_timezone)
    if errors:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "; ".join(errors))

    campaign.daily_start = body.daily_start or ""
    campaign.daily_end = body.daily_end or ""
    campaign.schedule_timezone = body.schedule_timezone or ""
    campaign.schedule_until = body.schedule_until
    if body.window_start is not None or body.window_end is not None:
        campaign.window_start = body.window_start
        campaign.window_end = body.window_end

    schedule = sched.from_campaign(campaign)
    if schedule is not None and campaign.status == CampaignStatus.DRAFT.value:
        # Giving a draft a clock is how you start it; requiring a separate
        # Start press afterwards is the manual step this replaces.
        campaign.status = CampaignStatus.SCHEDULED.value
    elif schedule is None and campaign.status == CampaignStatus.SCHEDULED.value:
        # Nothing left to wake it. Pause rather than draft: it may already have
        # candidates and results, which a draft never does.
        campaign.status = CampaignStatus.PAUSED.value

    session.add(
        Event(
            actor=user.username,
            kind="campaign_schedule_changed",
            campaign_id=campaign.id,
            payload={"summary": sched.describe(schedule), "status": campaign.status},
        )
    )
    await session.commit()
    await session.refresh(campaign)
    return campaign


class ForceStartRequest(BaseModel):
    hours: int = Field(
        8, ge=1, le=72,
        description="How long to run outside the schedule. Bounded because an "
        "override with no end is how a machine stays borrowed for a week.",
    )


@router.post(
    "/{campaign_id}/force-start",
    response_model=CampaignOut,
    summary="Run now, ignoring the schedule",
    description="Starts a campaign outside its nightly window and holds it open for "
    "`hours`. Without this, setting a scheduled campaign to active is undone on the "
    "next tick — the clock would put it straight back to sleep.",
)
async def force_start(
    campaign_id: int,
    body: ForceStartRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such campaign")

    now = datetime.now(UTC)
    until = now + timedelta(hours=body.hours)
    campaign.status = CampaignStatus.ACTIVE.value
    campaign.override_until = until
    # The window the rest of the loop reads. Writing it here is what makes the
    # override real: `_window_allows_new_run` and the hard cutoff both work off
    # these two fields and know nothing about overrides.
    campaign.window_start = now
    campaign.window_end = until
    session.add(
        Event(
            actor=user.username,
            kind="campaign_force_started",
            campaign_id=campaign.id,
            payload={"until": until.isoformat(), "hours": body.hours},
        )
    )
    await session.commit()
    await session.refresh(campaign)
    return campaign


@router.post(
    "/{campaign_id}/force-stop",
    response_model=CampaignOut,
    summary="Stop now, including runs in flight",
    description="Pauses the campaign and kills its live runs on the next worker tick, "
    "losing their measurements. Plain Pause is gentler: it stops new runs and lets the "
    "current ones finish.",
)
async def force_stop(
    campaign_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such campaign")

    if campaign.policy_id:
        # Stopping a policy campaign means "stop exploring NOW, still deliver
        # the verdict": the session is told to finalize, validation runs inside
        # what remains of the window, and the campaign stays ACTIVE until the
        # session ends it. Pausing here would freeze the container mid-night
        # with production down — the worst of both worlds. (To kill without a
        # verdict, abort the session on its detail page.)
        live = (
            await session.execute(
                select(PolicySession).where(
                    PolicySession.campaign_id == campaign_id,
                    PolicySession.status.not_in(
                        [s.value for s in TERMINAL_SESSION_STATES]
                    ),
                )
            )
        ).scalars().first()
        if live is not None:
            if live.finalize_requested_at is None:
                live.finalize_requested_at = datetime.now(UTC)
            session.add(
                Event(
                    actor=user.username, kind="policy_session_finalize_requested",
                    campaign_id=campaign_id,
                    payload={"session_id": live.id, "reason": "force stop"},
                )
            )
            await session.commit()
            await session.refresh(campaign)
            return campaign
        # No live session: fall through to the classic pause.

    campaign.status = CampaignStatus.PAUSED.value
    # Cleared, or the next tick would read the override and wake it again.
    campaign.override_until = None

    runs = (
        (
            await session.execute(
                select(Run).where(
                    Run.campaign_id == campaign_id,
                    Run.status.not_in(list(_TERMINAL_RUN_VALUES)),
                )
            )
        )
        .scalars()
        .all()
    )
    for run in runs:
        # The same request path the Stop button uses: the API never touches a
        # machine, so the teardown happens where every other teardown happens.
        session.add(
            Event(actor=user.username, kind="stop_requested", run_id=run.id,
                  campaign_id=campaign_id, payload={"reason": "force stop"})
        )
    session.add(
        Event(actor=user.username, kind="campaign_force_stopped",
              campaign_id=campaign_id, payload={"runs_stopped": len(runs)})
    )
    await session.commit()
    await session.refresh(campaign)
    return campaign


@router.post("/{campaign_id}/retry-failed")
async def retry_failed(
    campaign_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """Re-queue candidates whose runs all failed (e.g. after a platform fix).
    Candidates with a successful or still-live run are left alone."""
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such campaign")

    candidates = (
        (await session.execute(select(Candidate).where(Candidate.campaign_id == campaign_id)))
        .scalars()
        .all()
    )
    runs = (
        (await session.execute(select(Run).where(Run.campaign_id == campaign_id))).scalars().all()
    )
    runs_by_candidate: dict[int, list[Run]] = {}
    for run in runs:
        runs_by_candidate.setdefault(run.candidate_id, []).append(run)

    retried = 0
    for candidate in candidates:
        if candidate.status != "exhausted":
            continue
        if is_baseline_candidate(candidate):
            # A canary is not a candidate. Re-queuing one puts production's
            # config into the experiment queue, where the scheduler would
            # relaunch it on our GPUs. Retrying the canary is what restarting
            # the campaign does.
            continue
        candidate_runs = runs_by_candidate.get(candidate.id, [])
        if any(r.status == "succeeded" for r in candidate_runs):
            continue
        if any(r.status not in ("succeeded", "failed", "killed") for r in candidate_runs):
            continue  # still live
        candidate.status = "valid"
        retried += 1

    if retried and campaign.status == CampaignStatus.DONE.value:
        campaign.status = CampaignStatus.ACTIVE.value
    session.add(
        Event(
            actor=user.username,
            kind="candidates_retried",
            campaign_id=campaign_id,
            payload={"count": retried},
        )
    )
    await session.commit()
    return {"retried": retried}


@router.get("/{campaign_id}/candidates", response_model=list[CandidateOut])
async def list_candidates(
    campaign_id: int,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    candidates = (
        (
            await session.execute(
                select(Candidate).where(Candidate.campaign_id == campaign_id).order_by(Candidate.id)
            )
        )
        .scalars()
        .all()
    )
    # cards_used is the scheduler's own function: what the packing policy reads
    # is what the UI shows, rather than a second tp×dp×pp implementation.
    return [
        CandidateOut.model_validate(c).model_copy(
            update={"cards": 0 if is_in_place_baseline(c) else cards_used(c.config)}
        )
        for c in candidates
    ]


@router.get("/{campaign_id}/runs", response_model=list[RunOut])
async def list_campaign_runs(
    campaign_id: int,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    return (
        (
            await session.execute(
                select(Run)
                .options(selectinload(Run.candidate))  # RunOut.stage reads it
                .where(Run.campaign_id == campaign_id)
                .order_by(Run.id.desc())
            )
        )
        .scalars()
        .all()
    )


@router.get("/{campaign_id}/report", response_class=PlainTextResponse)
async def campaign_report(
    campaign_id: int,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """The morning report: what beat production, what broke, what to do next.
    Markdown, meant to be pasted into chat or a ticket."""
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such campaign")

    runs = (
        (
            await session.execute(
                select(Run)
                .options(selectinload(Run.candidate))
                .where(Run.campaign_id == campaign_id)
                .order_by(Run.id)
            )
        )
        .scalars()
        .all()
    )
    results = (
        (
            await session.execute(
                select(Result)
                .where(Result.run_id.in_([r.id for r in runs] or [0]), Result.source == "llmbench")
                .order_by(Result.id)
            )
        )
        .scalars()
        .all()
    )
    machines = {
        m.id: m for m in (await session.execute(select(Machine))).scalars().all()
    }
    return render_campaign_report(
        campaign=campaign,
        runs=list(runs),
        results_by_run={r.run_id: r for r in results},  # last wins
        machines=machines,
    )


@router.get("/{campaign_id}/parity")
async def config_parity(
    campaign_id: int,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """How this campaign's config differs from the production service captured
    on its machine — the reference that matters, since an engine default is not
    what the baseline canary measured."""
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such campaign")

    machines = (await session.execute(select(Machine))).scalars().all()
    allowed = campaign.machine_names or []
    candidates = [m for m in machines if not allowed or m.name in allowed]
    # Only a machine we have actually inspected can answer the question.
    machine = next((m for m in candidates if (m.baseline or {}).get("services")), None)
    if machine is None:
        return {"machine": "", "container": "", "missing": []}

    base = (campaign.search_space or {}).get("base") or {}
    config = {**base, **dict.fromkeys(swept_keys(campaign.search_space or {}))}
    missing = missing_flags(config, machine.baseline, campaign.served_model_name)
    return {
        "machine": machine.name,
        "container": missing[0]["container"] if missing else "",
        "missing": missing,
    }


@router.get("/{campaign_id}/leaderboard", response_model=list[LeaderboardEntry])
async def leaderboard(
    campaign_id: int,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """Successful runs ranked by objective target metric (fallback: LLMBench
    score_total)."""
    campaign = await session.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such campaign")

    rows = (
        await session.execute(
            select(Run, Candidate, Result)
            .join(Candidate, Run.candidate_id == Candidate.id)
            .join(Result, Result.run_id == Run.id)
            .where(
                Run.campaign_id == campaign_id,
                Run.status == "succeeded",
                Result.source == "llmbench",
            )
        )
    ).all()
    # The ordering itself lives in app/evaluation/ranking.py, because the
    # auto-promotion pass in the worker has to agree with this board about who
    # won — it opens a merge request for whatever is at the top of it.
    return leaderboard_entries(campaign, list(rows))
