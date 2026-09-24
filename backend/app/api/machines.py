import anyio
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.control.launch import MachineInfo, get_driver
from app.control.orchestrator.lifecycle import STEPS, describe_all
from app.core.auth import get_current_user
from app.core.config import get_settings
from app.db.base import get_async_session, sync_session_factory
from app.db.models import (
    TERMINAL_RUN_STATES,
    BaselineStatus,
    Campaign,
    CampaignStatus,
    Cluster,
    Event,
    Machine,
    MachineGroup,
    MachineGroupMember,
    MachineState,
    Run,
    RunNode,
    User,
)
from app.schemas.core import MachineCreate, MachineOut, MachineStateUpdate

router = APIRouter(prefix="/machines", tags=["machines"])


def _machine_info(machine: Machine) -> MachineInfo:
    return MachineInfo.of(machine)


def _driver_for(machine: Machine):
    """The driver that reaches this machine — its own substrate, not a global
    default, so a captured/cleared/restored k8s pool is handled by the k8s
    driver while the ssh boxes stay on ssh_docker. Empty falls back to the
    platform default inside get_driver."""
    return get_driver(machine.driver or "ssh_docker", cluster_id=machine.cluster_id)


async def _get_machine(machine_id: int, session: AsyncSession) -> Machine:
    machine = await session.get(Machine, machine_id)
    if machine is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such machine")
    return machine


async def _group_fields(
    session: AsyncSession, machine_ids: list[int]
) -> dict[int, tuple[str, int]]:
    """`machine_id -> (group name, rank)`. Membership is a topology, not state
    on the machine, so it is joined in rather than stored on the row."""
    if not machine_ids:
        return {}
    rows = (
        await session.execute(
            select(
                MachineGroupMember.machine_id, MachineGroup.name, MachineGroupMember.rank
            )
            .join(MachineGroup, MachineGroup.id == MachineGroupMember.group_id)
            .where(MachineGroupMember.machine_id.in_(machine_ids))
        )
    ).all()
    return {machine_id: (name, rank) for machine_id, name, rank in rows}


def _machine_out(
    machine: Machine,
    membership: tuple[str, int] | None = None,
    busy: int = 0,
    cluster_name: str = "",
) -> MachineOut:
    name, rank = membership or ("", None)
    return MachineOut.model_validate(machine).model_copy(
        update={"gpus_busy": busy, "group": name, "group_rank": rank, "cluster_name": cluster_name}
    )


async def _cluster_names(session: AsyncSession, cluster_ids: list[int | None]) -> dict[int, str]:
    """`cluster_id -> name`, for the machine list. A machine whose cluster_id is
    NULL is on the platform default cluster and gets an empty name."""
    ids = sorted({cid for cid in cluster_ids if cid})
    if not ids:
        return {}
    rows = (
        await session.execute(select(Cluster.id, Cluster.name).where(Cluster.id.in_(ids)))
    ).all()
    return {cid: name for cid, name in rows}


async def _one_out(session: AsyncSession, machine: Machine) -> MachineOut:
    membership = (await _group_fields(session, [machine.id])).get(machine.id)
    names = await _cluster_names(session, [machine.cluster_id])
    return _machine_out(machine, membership, cluster_name=names.get(machine.cluster_id or 0, ""))


@router.get("", response_model=list[MachineOut])
async def list_machines(
    _: User = Depends(get_current_user), session: AsyncSession = Depends(get_async_session)
):
    machines = (await session.execute(select(Machine).order_by(Machine.id))).scalars().all()
    # Busy cards come from run_nodes, per machine: a gang's worker row carries
    # its own cards, and its run row carries the master's.
    live_nodes = (
        (
            await session.execute(
                select(RunNode)
                .join(Run, Run.id == RunNode.run_id)
                .where(Run.status.not_in([s.value for s in TERMINAL_RUN_STATES]))
            )
        )
        .scalars()
        .all()
    )
    busy: dict[int, int] = {}
    for node in live_nodes:
        if node.machine_id is not None:
            busy[node.machine_id] = busy.get(node.machine_id, 0) + len(node.gpu_indices or [])
    membership = await _group_fields(session, [m.id for m in machines])
    cluster_names = await _cluster_names(session, [m.cluster_id for m in machines])
    return [
        _machine_out(
            m, membership.get(m.id), busy.get(m.id, 0), cluster_names.get(m.cluster_id or 0, "")
        )
        for m in machines
    ]


@router.get("/lifecycle")
async def machine_lifecycle(_: User = Depends(get_current_user)):
    """Where each machine sits in the hand-over sequence, and what moves it next.

    Answered by the same predicates the supervisor acts on, rather than by the
    page inferring it from `state` and `baseline_status`. Those two fields do
    not say whether a canary is owed, and a page that guesses is how manual
    Capture/Clear came to look like required steps.
    """
    settings = get_settings()

    def read() -> list[dict]:
        with sync_session_factory() as session:
            return [
                row.as_dict()
                for row in describe_all(
                    session,
                    settings.default_max_run_minutes,
                    auto=settings.auto_baseline_lifecycle,
                    auto_restore=settings.auto_restore_production,
                )
            ]

    return {
        "steps": list(STEPS),
        "auto": settings.auto_baseline_lifecycle,
        # Whether WE put production back when a lease ends. The page needs it
        # by name: with it off, "End lease" leaves production down, and that is
        # the one thing an operator must not learn afterwards.
        "auto_restore": settings.auto_restore_production,
        "machines": await anyio.to_thread.run_sync(read),
    }


async def _probe_capacity(machine: Machine) -> dict:
    """Ask the machine's substrate how many GPUs of what type it really has.
    Best-effort: a driver that cannot answer (bare metal, or an unconfigured/
    unreachable cluster) yields {"supported": False} and the caller keeps the
    values already on the row."""
    driver = _driver_for(machine)
    try:
        return await anyio.to_thread.run_sync(driver.probe_capacity, _machine_info(machine))
    except Exception as exc:  # a probe must never be the reason an add fails
        return {"supported": False, "warnings": [f"probe failed: {exc}"]}


def _apply_probe(machine: Machine, probe: dict) -> None:
    """Overwrite gpu_count/gpu_type from a successful probe. A probe that matched
    no nodes is left alone — it is a selector mistake, not a reason to zero the
    capacity the operator typed."""
    if probe.get("supported") and probe.get("node_count"):
        machine.gpu_count = probe.get("gpu_count", machine.gpu_count)
        if probe.get("gpu_type"):
            machine.gpu_type = probe["gpu_type"]


@router.get("/gpu-types")
async def gpu_types(_: User = Depends(get_current_user)):
    """The canonical GPU types the forms offer — one source of truth, so a card
    the cluster grows into is added in the backend and every dropdown follows."""
    from app.hardware import GPU_TYPES

    return {"gpu_types": list(GPU_TYPES)}


@router.post("", response_model=MachineOut)
async def create_machine(
    body: MachineCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    exists = (
        await session.execute(select(Machine).where(Machine.name == body.name))
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "machine name taken")
    machine = Machine(**body.model_dump())
    # A k8s machine is a node-slice, so its capacity and card type are the
    # cluster's to state, not the operator's to type. Read them from the node(s)
    # the selector matches; fall back to whatever was entered if the cluster
    # cannot be reached.
    probe = await _probe_capacity(machine) if machine.driver == "k8s" else {"supported": False}
    _apply_probe(machine, probe)
    session.add(machine)
    session.add(
        Event(
            actor=user.username,
            kind="machine_added",
            payload={"name": body.name, "probe": probe},
        )
    )
    await session.commit()
    await session.refresh(machine)
    return await _one_out(session, machine)


@router.post("/{machine_id}/probe-capacity")
async def probe_capacity(
    machine_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """Re-read GPU count and card type from the cluster and update the machine.

    The Refresh button behind a k8s node-slice: nodes get added, drained, or
    relabelled, so the capacity a machine was created with drifts. Returns the
    machine plus the raw probe (per-node detail and any warnings — a selector
    that matched nothing, an unknown card, a pool spanning two card types)."""
    machine = await _get_machine(machine_id, session)
    probe = await _probe_capacity(machine)
    if not probe.get("supported"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{machine.name} runs on the {machine.driver or 'ssh_docker'} substrate, "
            "which cannot probe capacity; set GPU count and type by hand",
        )
    _apply_probe(machine, probe)
    session.add(
        Event(
            actor=user.username,
            kind="machine_capacity_probed",
            payload={"machine": machine.name, "probe": probe},
        )
    )
    await session.commit()
    await session.refresh(machine)
    return {
        "machine": (await _one_out(session, machine)).model_dump(mode="json"),
        "probe": probe,
    }


async def _blocking_campaigns(session: AsyncSession, machine: Machine) -> list[str]:
    """Names of not-yet-finished campaigns pinned to this machine.

    A machine is not safe to edit or remove while a campaign that names it could
    still run on it — editing its capacity/selector would move the ground under a
    live search, and removing it (or renaming it) would break the pin. DONE
    campaigns are history and never block; a campaign with an empty machine list
    runs anywhere and pins nothing, so it does not count as "on this resource".
    """
    rows = (
        await session.execute(
            select(Campaign).where(Campaign.status != CampaignStatus.DONE.value)
        )
    ).scalars().all()
    return [c.name for c in rows if machine.name in (c.machine_names or [])]


def _guard_in_use(machine: Machine, blocking: list[str], verb: str) -> None:
    """Refuse the change if the machine is spoken for. Raised as 409 with the
    reason — the campaigns holding it, or a live run — so the UI can say what to
    finish first rather than just 'blocked'."""
    if blocking:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"cannot {verb} {machine.name}: {len(blocking)} active/draft campaign(s) "
            f"pinned to it ({', '.join(blocking)}). Finish or unpin them first.",
        )
    if machine.state == MachineState.RESERVED.value:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"cannot {verb} {machine.name}: a live run holds it; stop the run first",
        )


@router.put("/{machine_id}", response_model=MachineOut)
async def update_machine(
    machine_id: int,
    body: MachineCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """Edit a machine's fields. Blocked while a not-yet-finished campaign is
    pinned to it (its capacity/placement must not move under a live search, and
    a rename would break the pin). A k8s machine's capacity/type is re-read from
    the cluster after the edit, same as on create."""
    machine = await _get_machine(machine_id, session)
    _guard_in_use(machine, await _blocking_campaigns(session, machine), "edit")
    if body.name != machine.name:
        clash = (
            await session.execute(
                select(Machine).where(Machine.name == body.name, Machine.id != machine_id)
            )
        ).scalar_one_or_none()
        if clash is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "machine name taken")
    for field, value in body.model_dump().items():
        setattr(machine, field, value)
    probe = await _probe_capacity(machine) if machine.driver == "k8s" else {"supported": False}
    _apply_probe(machine, probe)
    session.add(
        Event(
            actor=user.username,
            kind="machine_updated",
            payload={"name": machine.name, "probe": probe},
        )
    )
    await session.commit()
    await session.refresh(machine)
    return await _one_out(session, machine)


@router.delete("/{machine_id}")
async def delete_machine(
    machine_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """Remove a machine. Blocked while a not-yet-finished campaign is pinned to
    it, or a live run holds it. Historical runs are kept (their results and
    hardware provenance live on the run itself); their machine link is nulled so
    the row can go without erasing the night it measured."""
    machine = await _get_machine(machine_id, session)
    _guard_in_use(machine, await _blocking_campaigns(session, machine), "remove")
    name = machine.name
    # Detach finished runs rather than cascade-deleting them: the measurement is
    # the point of the platform, and env_snapshot already carries the card/node.
    # The per-node rows are detached too — a node's cards and command are the
    # measurement's provenance, so they outlive the fleet entry as well.
    await session.execute(
        update(Run).where(Run.machine_id == machine_id).values(machine_id=None)
    )
    await session.execute(
        update(RunNode).where(RunNode.machine_id == machine_id).values(machine_id=None)
    )
    await session.delete(machine)
    session.add(Event(actor=user.username, kind="machine_removed", payload={"name": name}))
    await session.commit()
    return {"removed": name}


@router.post("/{machine_id}/baseline/capture", response_model=MachineOut)
async def capture_baseline(
    machine_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """Record the production services we were handed, and how to restore them.

    This is the safety interlock for `clear`: nothing may be torn down until
    it has been captured.
    """
    machine = await _get_machine(machine_id, session)
    driver = _driver_for(machine)
    try:
        baseline = await anyio.to_thread.run_sync(driver.capture_baseline, _machine_info(machine))
    except Exception as exc:  # ssh/docker failure
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"capture failed: {exc}") from exc

    machine.baseline = baseline
    machine.baseline_status = BaselineStatus.CAPTURED.value
    session.add(
        Event(
            actor=user.username,
            kind="baseline_captured",
            payload={"machine": machine.name, "services": len(baseline.get("services", []))},
        )
    )
    await session.commit()
    await session.refresh(machine)
    return await _one_out(session, machine)


@router.post("/{machine_id}/baseline/clear", response_model=MachineOut)
async def clear_baseline(
    machine_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """Stop the production services so experiments can have the machine.
    Refuses unless a baseline was captured — never destroy the unrecoverable."""
    machine = await _get_machine(machine_id, session)
    if machine.baseline_status != BaselineStatus.CAPTURED.value:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"baseline must be captured first (status: {machine.baseline_status})",
        )
    driver = _driver_for(machine)
    try:
        stopped = await anyio.to_thread.run_sync(
            driver.clear_baseline, _machine_info(machine), machine.baseline
        )
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"clear failed: {exc}") from exc

    machine.baseline_status = BaselineStatus.CLEARED.value
    session.add(
        Event(
            actor=user.username,
            kind="baseline_cleared",
            payload={"machine": machine.name, "stopped": stopped},
        )
    )
    await session.commit()
    await session.refresh(machine)
    return await _one_out(session, machine)


@router.post("/{machine_id}/baseline/restore", response_model=MachineOut)
async def restore_baseline(
    machine_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """Put production back before hand-back (re-runs the captured deploy
    scripts). Refuses while a run still holds the machine."""
    machine = await _get_machine(machine_id, session)
    if machine.state == MachineState.RESERVED.value:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "a run still holds this machine; stop it first"
        )
    if not (machine.baseline or {}).get("services"):
        raise HTTPException(status.HTTP_409_CONFLICT, "no captured baseline to restore")

    driver = _driver_for(machine)
    try:
        restored = await anyio.to_thread.run_sync(
            driver.restore_baseline, _machine_info(machine), machine.baseline
        )
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"restore failed: {exc}") from exc

    # Restoring is not the same as restoring faithfully: verify what came back
    # against what we captured, and surface any drift loudly.
    try:
        findings = await anyio.to_thread.run_sync(
            driver.verify_baseline, _machine_info(machine), machine.baseline
        )
    except Exception as exc:  # verification is advisory, never fatal
        findings = [{"ok": False, "detail": f"verification unavailable: {exc}"}]

    drift = [f for f in findings if not f.get("ok")]
    machine.baseline_status = BaselineStatus.RESTORED.value
    machine.baseline = {**machine.baseline, "restore_verification": findings}
    session.add(
        Event(
            actor=user.username,
            kind="baseline_restored" if not drift else "baseline_restored_with_drift",
            payload={"machine": machine.name, "restored": restored, "verification": findings},
        )
    )
    await session.commit()
    await session.refresh(machine)
    if drift:
        # 200 with the machine body would bury this; the operator must see it.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "restored, but the result does not match the capture: "
            + "; ".join(f"port {f.get('port')}: {f.get('detail')}" for f in drift),
        )
    return await _one_out(session, machine)


@router.put("/{machine_id}/state", response_model=MachineOut)
async def set_machine_state(
    machine_id: int,
    body: MachineStateUpdate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """The manual borrow/return ritual: a human marks
    the machine as handed to the platform (available) or taken back (away)."""
    if body.state not in (MachineState.AWAY.value, MachineState.AVAILABLE.value):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "state must be away|available")
    machine = await session.get(Machine, machine_id)
    if machine is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such machine")
    if machine.state == MachineState.RESERVED.value:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "machine is reserved by a live run; wait for it to finish or kill the run",
        )
    machine.state = body.state
    session.add(
        Event(
            actor=user.username,
            kind="machine_state_changed",
            payload={"machine": machine.name, "state": body.state},
        )
    )
    await session.commit()
    await session.refresh(machine)
    return await _one_out(session, machine)
