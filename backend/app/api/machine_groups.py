"""Node groups — the multi-node resource.

A group is a named, ordered set of leased machines that can be deployed as one
gang, with a master. Creating one is an operator action on the Resources page;
pinning one is a campaign or (later) a Baseline Hub submission.

The distinction this file has to keep straight, because it is the whole reason
groups are safe: **a group is a topology, not a reservation.** A member is still
an ordinary machine. It stays in the single-node fleet, it may be leased and
handed back individually, and it is occupied only while a gang is actually live
on it. So none of these endpoints writes a lock, and `deployable` is a statement
about right now (computed from leases, hand-over state and live runs) rather
than a stored flag.

Following the lease API's lead, machines are addressed by NAME: the caller knows
its own fleet's names, and every other pin in the platform (a campaign's
`machine_names`) speaks names too.
"""

import anyio
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.control.launch import MachineInfo, get_driver
from app.control.launch import preflight as pf
from app.control.orchestrator.groups import (
    busy_cards,
    group_blockers,
    group_lease_warnings,
    members_by_rank,
)
from app.core.auth import get_current_user
from app.db.base import get_async_session, sync_session_factory
from app.db.models import (
    TERMINAL_RUN_STATES,
    Campaign,
    CampaignStatus,
    Event,
    LeaseState,
    Machine,
    MachineGroup,
    MachineGroupMember,
    Run,
    User,
)
from app.schemas.core import (
    MachineGroupCreate,
    MachineGroupMemberOut,
    MachineGroupOut,
    MachineGroupUpdate,
)

router = APIRouter(prefix="/machine-groups", tags=["node groups"])

# Default substrate when a machine names none, matching `get_driver` and the
# Machine column's convention. Two members on different substrates cannot form
# one deployment, so this is the value the homogeneity check compares.
_DEFAULT_DRIVER = "ssh_docker"


class GroupPreflightRequest(BaseModel):
    """A deployment as it would run, so the group can be checked against it.

    Takes a draft rather than a saved campaign or submission — the whole value
    is finding out before a night is committed, and both callers have these
    fields already.
    """

    image: str = ""
    model_path: str = ""
    service_port: int = 28200
    volumes: dict[str, str] = Field(default_factory=dict)


def _effective_driver(machine: Machine) -> str:
    return machine.driver or _DEFAULT_DRIVER


async def _get_group(group_id: int, session: AsyncSession) -> MachineGroup:
    group = await session.get(MachineGroup, group_id)
    if group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such node group")
    return group


# -- validation ---------------------------------------------------------------


async def _load_members(
    session: AsyncSession, names: list[str], *, exclude_group_id: int | None = None
) -> list[Machine]:
    """The machines these names refer to, validated as a deployable set.

    Raises 422 for anything the operator can fix by editing the group, 409 for a
    genuine conflict (a box already committed to another group). Deliberately
    does NOT require the members to be leased: a lease is nightly and a group is
    a standing topology, so "not leased yet" belongs in the group's `blockers`
    (which the page shows) and not in a refusal that would force the group to be
    deleted and recreated every evening.
    """
    cleaned = [name.strip() for name in names if name and name.strip()]
    if not cleaned:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "a group needs members")
    duplicates = sorted({name for name in cleaned if cleaned.count(name) > 1})
    if duplicates:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"duplicate member(s): {', '.join(duplicates)} — a machine has one rank",
        )

    machines = list(
        (await session.execute(select(Machine).where(Machine.name.in_(cleaned)))).scalars()
    )
    by_name = {machine.name: machine for machine in machines}
    missing = [name for name in cleaned if name not in by_name]
    if missing:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"not registered on the Resources page: {', '.join(missing)}",
        )
    # Rank order is the caller's, so return in the order given, not the DB's.
    ordered = [by_name[name] for name in cleaned]

    drivers = {_effective_driver(machine) for machine in ordered}
    if len(drivers) > 1:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "a node group must be one substrate; these machines span "
            f"{', '.join(sorted(drivers))}",
        )
    # A gang's pods reach each other over the cluster network, so members must
    # live in the same cluster even when they share a substrate. NULL is the
    # platform-default cluster and is a value like any other here.
    cluster_ids = {machine.cluster_id for machine in ordered}
    if len(cluster_ids) > 1:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "a node group must be in one cluster; these machines span "
            f"{', '.join(sorted(str(c) if c else 'default' for c in cluster_ids))}",
        )
    card_types = {(machine.gpu_type or "") for machine in ordered}
    if len(card_types) > 1:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "a node group must be one GPU type; these machines are "
            f"{', '.join(sorted(t or '(unset)' for t in card_types))}",
        )
    card_counts = {machine.gpu_count for machine in ordered}
    if len(card_counts) > 1:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "a node group must have the same card count on every member; these are "
            f"{', '.join(str(c) for c in sorted(card_counts))}",
        )

    machine_ids = [machine.id for machine in ordered]
    taken = (
        await session.execute(
            select(MachineGroupMember.machine_id, MachineGroup.name)
            .join(MachineGroup, MachineGroup.id == MachineGroupMember.group_id)
            .where(
                MachineGroupMember.machine_id.in_(machine_ids),
                MachineGroupMember.group_id != (exclude_group_id or -1),
            )
        )
    ).all()
    if taken:
        name_of = {machine.id: machine.name for machine in ordered}
        where = ", ".join(
            f"{name_of.get(machine_id, machine_id)} is in {group}"
            for machine_id, group in taken
        )
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"a machine can belong to only one node group ({where})",
        )
    return ordered


async def _assert_driver_matches(
    session: AsyncSession, machines: list[Machine], driver: str
) -> None:
    """An explicit group `driver` must be the one every member is actually on."""
    if not driver:
        return
    wrong = [m.name for m in machines if _effective_driver(m) != driver]
    if wrong:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"driver {driver!r} does not match {', '.join(wrong)}",
        )


# -- serialization ------------------------------------------------------------


def _member_out(machine: Machine, rank: int, busy: int) -> MachineGroupMemberOut:
    return MachineGroupMemberOut(
        name=machine.name,
        rank=rank,
        is_master=(rank == 0),
        host=machine.host,
        data_host=machine.data_host or machine.host,
        gpu_count=machine.gpu_count,
        gpu_type=machine.gpu_type,
        driver=_effective_driver(machine),
        leased=machine.lease_state == LeaseState.ACTIVE.value,
        state=machine.state,
        baseline_status=machine.baseline_status,
        needs_attention=bool(machine.needs_attention),
        gpus_busy=busy,
        lease_due_at=machine.lease_due_at,
    )


def _serialize(session, group: MachineGroup) -> MachineGroupOut:
    """Build the response from the live fleet state, inside a sync session.

    The derived fields (`leased`, `gpus_busy`, `deployable`, `blockers`) are
    computed from the same predicates the supervisor acts on, so the page cannot
    promise a deployment the scheduler would refuse.
    """
    ranked = members_by_rank(session, group)
    busy = busy_cards(session, [machine.id for _, machine in ranked])
    members = [
        _member_out(machine, rank, busy.get(machine.id, 0)) for rank, machine in ranked
    ]
    blockers = group_blockers(session, group)
    return MachineGroupOut(
        id=group.id,
        name=group.name,
        driver=group.driver,
        cluster_id=group.cluster_id,
        nccl_env=group.nccl_env or {},
        dist_port=group.dist_port,
        extra_env=group.extra_env or {},
        extra_volumes=group.extra_volumes or {},
        notes=group.notes,
        members=members,
        node_count=len(members),
        deployable=not blockers,
        blockers=blockers,
        warnings=group_lease_warnings(session, group),
        created_at=group.created_at,
    )


def _read_groups() -> list[dict]:
    with sync_session_factory() as session:
        groups = session.scalars(select(MachineGroup).order_by(MachineGroup.id)).all()
        return [_serialize(session, group).model_dump(mode="json") for group in groups]


def _read_group(group_id: int) -> dict:
    with sync_session_factory() as session:
        group = session.get(MachineGroup, group_id)
        if group is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such node group")
        return _serialize(session, group).model_dump(mode="json")


# -- endpoints ----------------------------------------------------------------


@router.get("", response_model=list[MachineGroupOut])
async def list_groups(_: User = Depends(get_current_user)):
    return await anyio.to_thread.run_sync(_read_groups)


@router.get("/{group_id}", response_model=MachineGroupOut)
async def get_group(group_id: int, _: User = Depends(get_current_user)):
    return await anyio.to_thread.run_sync(_read_group, group_id)


@router.post("", response_model=MachineGroupOut, status_code=status.HTTP_201_CREATED)
async def create_group(
    body: MachineGroupCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """Form a group from leased machines. `members[0]` is the master.

    Nothing is reserved and no machine changes state: this is the operator
    declaring a topology, which the scheduler may use when all members happen to
    be free.
    """
    name = body.name.strip()
    if not name:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "a group needs a name")
    clash = (
        await session.execute(select(MachineGroup).where(MachineGroup.name == name))
    ).scalar_one_or_none()
    if clash is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "node group name taken")

    machines = await _load_members(session, body.members)
    await _assert_driver_matches(session, machines, body.driver)

    group = MachineGroup(
        name=name,
        driver=body.driver,
        cluster_id=next((m.cluster_id for m in machines if m.cluster_id), None),
        nccl_env=body.nccl_env,
        dist_port=body.dist_port,
        extra_env=body.extra_env,
        extra_volumes=body.extra_volumes,
        notes=body.notes,
    )
    session.add(group)
    await session.flush()
    for rank, machine in enumerate(machines):
        session.add(MachineGroupMember(group_id=group.id, machine_id=machine.id, rank=rank))
    session.add(
        Event(
            actor=user.username,
            kind="machine_group_created",
            payload={
                "group": name,
                "members": [machine.name for machine in machines],
                "master": machines[0].name,
            },
        )
    )
    await session.commit()
    return await anyio.to_thread.run_sync(_read_group, group.id)


@router.put("/{group_id}", response_model=MachineGroupOut)
async def update_group(
    group_id: int,
    body: MachineGroupUpdate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """Edit a group. Replacing `members` re-ranks the whole set, so `members[0]`
    is the new master — the master is a position, not a separate field that
    could disagree with the rank order."""
    group = await _get_group(group_id, session)
    fields = body.model_dump(exclude_unset=True)

    if "members" in fields and fields["members"] is not None:
        machines = await _load_members(
            session, fields.pop("members"), exclude_group_id=group.id
        )
        await _assert_driver_matches(session, machines, fields.get("driver", group.driver))
        await session.execute(
            delete(MachineGroupMember).where(MachineGroupMember.group_id == group.id)
        )
        for rank, machine in enumerate(machines):
            session.add(MachineGroupMember(group_id=group.id, machine_id=machine.id, rank=rank))
        group.cluster_id = next((m.cluster_id for m in machines if m.cluster_id), None)
    else:
        fields.pop("members", None)
        # A driver edit must still describe the machines it now covers.
        if fields.get("driver"):
            ranked = (
                await session.execute(
                    select(Machine)
                    .join(MachineGroupMember, MachineGroupMember.machine_id == Machine.id)
                    .where(MachineGroupMember.group_id == group.id)
                )
            ).scalars().all()
            await _assert_driver_matches(session, list(ranked), fields["driver"])

    for field, value in fields.items():
        if value is not None:
            setattr(group, field, value)
    session.add(
        Event(
            actor=user.username,
            kind="machine_group_updated",
            payload={"group": group.name, "fields": sorted(fields)},
        )
    )
    await session.commit()
    return await anyio.to_thread.run_sync(_read_group, group.id)


@router.post("/{group_id}/preflight")
async def preflight_group(
    group_id: int,
    body: GroupPreflightRequest,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """Check a group the way a deployment would use it: every member probed, and
    the interconnect findings a gang adds.

    Cheap checks for expensive failures — the boxes can each be perfect and still
    be unable to reach each other, which nothing in a per-machine view shows.
    Runs in a worker thread: it is ssh, and one machine's probe is not the
    event loop's business.
    """
    group = await _get_group(group_id, session)
    ranked = (
        await session.execute(
            select(MachineGroupMember.rank, Machine)
            .join(Machine, Machine.id == MachineGroupMember.machine_id)
            .where(MachineGroupMember.group_id == group.id)
            .order_by(MachineGroupMember.rank)
        )
    ).all()
    if not ranked:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{group.name} has no members")
    infos = [(rank, MachineInfo.of(machine)) for rank, machine in ranked]
    drivers = {info.driver or "ssh_docker" for _, info in infos}
    if drivers - {"ssh_docker"}:
        # A k8s slice has no machine to ssh into; its placement is cluster-side
        # and the driver says so rather than pretending a probe happened.
        return {
            "machines": [
                pf.Preflight(name, [pf.Check(
                    "substrate", "Deployment substrate", pf.SKIP,
                    f"{name} runs on the {info.driver} substrate; readiness is "
                    "decided cluster-side",
                    "Machine-side preflight (ssh, ports, fabric) only applies to "
                    "bare-metal hosts.",
                )]).as_dict()
                for (_, info) in infos
                for name in [info.name]
            ],
            "ok": True,
            "note": "cluster-scheduled group: no machine-side checks apply",
        }
    driver = get_driver(_effective_driver(ranked[0][1]))
    results = await anyio.to_thread.run_sync(
        lambda: pf.inspect_group(
            driver, infos,
            image=body.image, model_path=body.model_path,
            volumes={**group.extra_volumes, **body.volumes},
            service_port=body.service_port, nccl_env=group.nccl_env or {},
        )
    )
    return {
        "machines": results,
        "ok": all(row["ok"] for row in results),
        "note": "",
    }


@router.delete("/{group_id}")
async def delete_group(
    group_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """Dissolve a group. Never touches the machines — they simply go back to
    being loose members of the fleet, which they always were."""
    group = await _get_group(group_id, session)
    pinned = (
        await session.execute(
            select(Campaign.name).where(
                Campaign.node_group == group.name,
                Campaign.status != CampaignStatus.DONE.value,
            )
        )
    ).scalars().all()
    if pinned:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"cannot remove {group.name}: {len(pinned)} active/draft campaign(s) "
            f"deploy across it ({', '.join(pinned)}). Unpin them first.",
        )
    live = (
        await session.execute(
            select(Run.id).where(
                Run.node_group == group.name,
                Run.status.not_in([state.value for state in TERMINAL_RUN_STATES]),
            ).limit(1)
        )
    ).first()
    if live is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"cannot remove {group.name}: a multi-node run is live on it; stop it first",
        )
    name = group.name
    await session.delete(group)
    session.add(Event(actor=user.username, kind="machine_group_removed", payload={"group": name}))
    await session.commit()
    return {"removed": name}
