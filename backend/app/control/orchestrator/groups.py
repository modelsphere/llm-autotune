"""Node groups: which machines a gang would use, and whether it could run now.

A group is a named, ordered set of machines that can be deployed as one
multi-node service. These helpers are the one place that resolves a group name
into machines, computes whether it is ready to deploy, and says why not — shared
by the API that manages groups and, from step 5, the scheduler that places them.

The property that must not be lost: a group is a topology, NOT a reservation.
Nothing here writes state, and "blocked" is a statement about right now — a
member running single-node work is a reason the gang waits, not a reason the
member is unavailable.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.control.orchestrator.lifecycle import accepts_new_work, machine_has_live_session
from app.control.run_nodes import machine_hosts_live_gang
from app.db.models import (
    TERMINAL_RUN_STATES,
    BaselineStatus,
    Machine,
    MachineGroup,
    MachineGroupMember,
    MachineState,
    Run,
    RunNode,
)


def resolve_group(session: Session, name: str) -> MachineGroup | None:
    """The group a pin names, or None. Empty name is not a group."""
    if not name:
        return None
    return session.scalars(
        select(MachineGroup).where(MachineGroup.name == name)
    ).first()


def members_by_rank(session: Session, group: MachineGroup) -> list[tuple[int, Machine]]:
    """`(rank, machine)` in rank order, master (rank 0) first.

    Reads the join row's rank rather than `group.members`' insertion order: the
    launch order is the whole point of storing a rank, and a relationship that
    reordered itself would silently produce a different gang.
    """
    rows = session.execute(
        select(MachineGroupMember.rank, Machine)
        .join(Machine, Machine.id == MachineGroupMember.machine_id)
        .where(MachineGroupMember.group_id == group.id)
        .order_by(MachineGroupMember.rank)
    ).all()
    return [(rank, machine) for rank, machine in rows]


def member_names(session: Session, group: MachineGroup) -> list[str]:
    return [machine.name for _, machine in members_by_rank(session, group)]


def busy_cards(session: Session, machine_ids: list[int]) -> dict[int, int]:
    """Live cards per machine, from the run NODES — a gang's worker row carries
    its own cards while the run row carries the master's."""
    if not machine_ids:
        return {}
    out: dict[int, int] = {}
    for node in session.scalars(
        select(RunNode)
        .join(Run, Run.id == RunNode.run_id)
        .where(
            RunNode.machine_id.in_(machine_ids),
            Run.status.not_in([state.value for state in TERMINAL_RUN_STATES]),
        )
    ).all():
        out[node.machine_id] = out.get(node.machine_id, 0) + len(node.gpu_indices or [])
    return out


def member_blockers(session: Session, machine: Machine) -> list[str]:
    """Why this member could not be part of a gang starting RIGHT NOW.

    Every entry is time-scoped and none of them touches the machine: a blocker
    is "wait", not "unavailable". An empty list means the member is idle, ours
    and handed over.
    """
    blockers: list[str] = []
    if machine.needs_attention:
        blockers.append(
            f"{machine.name} is quarantined"
            + (f": {machine.attention_reason}" if machine.attention_reason else "")
        )
    if machine.state == MachineState.AWAY.value:
        blockers.append(f"{machine.name} is not leased")
    elif not accepts_new_work(machine):
        blockers.append(f"{machine.name} is being handed back")
    if machine.baseline_status != BaselineStatus.CLEARED.value:
        blockers.append(f"{machine.name}'s production has not been cleared")
    if machine_has_live_session(session, machine):
        blockers.append(f"{machine.name} is hosting a policy session")
    if machine_hosts_live_gang(session, machine.id):
        blockers.append(f"{machine.name} is already part of a live multi-node run")
    live = session.scalars(
        select(Run)
        .where(
            Run.id.in_(
                select(RunNode.run_id).where(RunNode.machine_id == machine.id)
            ),
            Run.status.not_in([state.value for state in TERMINAL_RUN_STATES]),
        )
        .limit(1)
    ).first()
    if live is not None:
        blockers.append(f"{machine.name} is running work")
    return blockers


def group_blockers(session: Session, group: MachineGroup) -> list[str]:
    """Every reason the group could not deploy right now, member by member.

    Flattened rather than keyed by member because the page shows it as a list of
    things to fix, and a group is deployable only when the list is empty.
    """
    out: list[str] = []
    for _, machine in members_by_rank(session, group):
        out.extend(member_blockers(session, machine))
    return out


# How far ahead a lease expiry is worth warning about. A gang has no single run
# duration to compare against — the campaign's `max_run_minutes` is an upper
# bound on ONE run, and a night is many — so the horizon is "tonight".
LEASE_WARNING_HORIZON = timedelta(hours=24)


def group_lease_warnings(session: Session, group: MachineGroup) -> list[str]:
    """Leases that run out within the horizon.

    Not a blocker: a lease due at 08:00 is exactly right for a night's work. It
    is worth saying because a gang is cut short by whichever member is handed
    back first, and that is not obvious from a per-machine list.
    """
    ranked = members_by_rank(session, group)
    warnings: list[str] = []
    horizon = datetime.now(UTC) + LEASE_WARNING_HORIZON
    for _, machine in ranked:
        due = machine.lease_due_at
        if due is None:
            continue
        if due.tzinfo is None:
            due = due.replace(tzinfo=UTC)
        if due <= horizon:
            warnings.append(
                f"{machine.name}'s lease is due {due:%Y-%m-%d %H:%M} UTC — a run that "
                "spans the group is cut short when any member goes back"
            )
    return warnings


def group_deployable(session: Session, group: MachineGroup) -> bool:
    return not group_blockers(session, group)
