"""The run state machine. Runs are rows advanced by the supervisor — never
tasks in a queue (tech-stack.md). Keep this the single source of truth for
what transitions are legal."""

from app.db.models import RunStatus

ALLOWED_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.PENDING: {RunStatus.LAUNCHING, RunStatus.KILLED, RunStatus.FAILED},
    # LAUNCHING may skip WAITING_READY: a service can be ready on the very
    # first poll (tiny models, mock engine with no startup delay).
    RunStatus.LAUNCHING: {
        RunStatus.WAITING_READY,
        RunStatus.HEALTH_CHECK,
        RunStatus.FAILED,
        RunStatus.KILLED,
    },
    RunStatus.WAITING_READY: {RunStatus.HEALTH_CHECK, RunStatus.FAILED, RunStatus.KILLED},
    # HEALTH_CHECK → SERVING is the delegated-launch path: a POLICY_LAUNCH run
    # that passes the health gate is held up for its policy instead of being
    # benchmarked and torn down.
    RunStatus.HEALTH_CHECK: {
        RunStatus.BENCHING,
        RunStatus.SERVING,
        RunStatus.FAILED,
        RunStatus.KILLED,
    },
    RunStatus.BENCHING: {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.KILLED},
    # SERVING → SUCCEEDED is the policy releasing its engine; the run "worked"
    # even though nothing was benchmarked under this run id (benchmarks of a
    # held engine are their own EXTERNAL runs).
    RunStatus.SERVING: {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.KILLED},
    RunStatus.SUCCEEDED: set(),
    RunStatus.FAILED: set(),
    RunStatus.KILLED: set(),
}


def can_transition(current: RunStatus | str, target: RunStatus | str) -> bool:
    return RunStatus(target) in ALLOWED_TRANSITIONS[RunStatus(current)]
