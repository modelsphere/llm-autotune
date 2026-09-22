"""Which machines a run occupies — the questions multi-node made non-trivial.

Before multi-node, "the machines of a run" was `runs.machine_id`, a single
column, and every machine-liveness query read it directly. A gang makes that
answer a set, and the set lives in `run_nodes`. These helpers exist so the
queries that must be gang-correct ask one place instead of each growing their
own join and drifting.

They are SELECT builders and small sync helpers, so the sync orchestrator and
the async API can use the same predicate. Nothing here writes: `RunNode` rows
are created and kept in lockstep with their run by the mapper events in
`app.db.models`.
"""

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.db.models import TERMINAL_RUN_STATES, Run, RunNode


def run_ids_on_machine(machine_id: int) -> Select:
    """Select of run ids that occupy this machine, as master OR worker.

    `.in_()`-able, which is how a query that used `Run.machine_id == id` becomes
    gang-correct without changing its shape.
    """
    return select(RunNode.run_id).where(RunNode.machine_id == machine_id)


def live_run_ids_on_machine(machine_id: int) -> Select:
    """The same, restricted to runs that are not finished."""
    return (
        select(RunNode.run_id)
        .join(Run, Run.id == RunNode.run_id)
        .where(
            RunNode.machine_id == machine_id,
            Run.status.not_in([state.value for state in TERMINAL_RUN_STATES]),
        )
    )


def nodes_of(session: Session, run_id: int) -> list[RunNode]:
    """A run's nodes, master first.

    Read from the database rather than `run.run_nodes`: the mapper events write
    rank 0 through the connection, so a collection loaded earlier in the same
    session would be a stale view of a row that certainly exists.
    """
    return list(
        session.scalars(
            select(RunNode).where(RunNode.run_id == run_id).order_by(RunNode.rank)
        ).all()
    )


def node_count(session: Session, run_id: int) -> int:
    return (
        session.scalars(
            select(func.count(RunNode.id)).where(RunNode.run_id == run_id)
        ).first()
        or 0
    )


def machine_hosts_live_gang(session: Session, machine_id: int) -> bool:
    """Is this machine part of a live MULTI-node run right now?

    This is the half of the grouping contract that has to be enforced from the
    single-node side. A node group is a named topology, not a reservation: its
    members stay ordinary machines the scheduler may hand to single-node work,
    and a member is taken only while a gang is actually deployed on it. So the
    rule is time-scoped, not ownership-scoped —

        * a member with no gang on it is available for single-node runs;
        * while a gang is live on it, nothing else may be placed there, even on
          cards the gang is not using. A gang's ranks share an interconnect and
          a host, and a co-tenant engine would contend for both, which is
          exactly the skew the measurement cannot afford.

    Deliberately reads live runs only: a gang that finished an hour ago leaves
    its nodes free for whatever runs next, which is the "different time
    windows" half of the requirement.
    """
    # Runs that have more than one node — computed rather than stored, because a
    # stored flag would be a second answer that could disagree with the rows.
    gang_run_ids = (
        select(RunNode.run_id)
        .group_by(RunNode.run_id)
        .having(func.count(RunNode.id) > 1)
    )
    return (
        session.scalars(
            select(RunNode.id)
            .join(Run, Run.id == RunNode.run_id)
            .where(
                RunNode.machine_id == machine_id,
                Run.status.not_in([state.value for state in TERMINAL_RUN_STATES]),
                RunNode.run_id.in_(gang_run_ids),
            )
            .limit(1)
        ).first()
        is not None
    )
