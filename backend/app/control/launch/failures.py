"""Why a run died, in substrate-neutral terms.

These rules used to live in the ssh+docker driver, which made the orchestrator
import a *concrete* driver to interpret failures — the one place it reached
past the DeploymentDriver interface. A k8s driver would then have had its
container deaths classified by rules named after docker.

The two predicates matter more than the classifier. Everything downstream —
whether to retry, whether a search should learn from it — turns on one
question: did the CONFIGURATION fail, or did the infrastructure?
"""

# The machine, the network or the registry misbehaved. A config is only ever
# condemned by its own behaviour, never by flaky infrastructure, so these are
# retried on a fresh placement and kept out of a search's model.
INFRASTRUCTURE_FAILURES = frozenset(
    {
        "ssh_timeout",
        "image_missing",
        "image_pull",
        "gone",
        "orphaned",
        "killed_externally",
        # Another service grabbed the port between placement and launch. The
        # config is blameless, and the next attempt gets a fresh port.
        "port_conflict",
        # A card the scheduler picked was still physically busy at launch — a
        # previous run's container had not finished dying. The config is
        # blameless; the next attempt (once the janitor confirms the card free)
        # gets clean hardware instead of a spurious model-load crash.
        "gpu_busy",
        # k8s placement problems: the scheduler put the pod where the model's
        # hostPath is not present (mount_failed), no node fit it (unschedulable),
        # or the sandbox could not be created. The parameters are blameless —
        # the next attempt (with a corrected nodeSelector, or as the cluster
        # frees a fitting node) can succeed. Bounded by MAX_ATTEMPTS_PER_CANDIDATE.
        "mount_failed",
        "unschedulable",
        "sandbox_failed",
        # A pod no node could EVER take: a node selector that matches nothing,
        # or more cards than the biggest matched node has. Blameless like its
        # siblings — the machine is misdescribed, not the config — but unlike
        # "unschedulable" it will not come true by waiting, so it fails the run
        # at once instead of queueing.
        "unplaceable",
    }
)

# Not failures at all: the substrate has not placed the run YET. A queued pod
# schedules the moment a node frees the cards it asked for — which happens
# routinely between our own rounds and whenever production holds the cluster —
# so this reads as "still coming up" and never ends a run. Leasing a k8s pool
# buys the right to ASK for a schedule, not a guarantee of one, and waiting is
# what asking looks like. The outer bound is the machine's lease, not a timer:
# when the lease ends the run is drained like any other.
TRANSIENT_PLACEMENT_FAILURES = frozenset({"unschedulable"})

# Not a failure either, and not the substrate's doing: WE withdrew the run.
# A paused campaign gives its unplaced runs back so they stop competing for
# cards, and a withdrawn run must not count against a candidate's retry budget
# — nothing was learned and nothing went wrong.
RELEASED = "released"

# The configuration itself is at fault: these reproduce if you run the same
# parameters again, which makes them genuine observations — an optimizer
# should learn to avoid the region rather than treat the point as missing.
CONFIG_FAILURES = frozenset(
    {
        "oom",
        "bad_config",
        "benchmark_not_passed",
        "ready_timeout",
        "nccl",
        "segfault",
        "model_load",
    }
)

# Deliberately in NEITHER set:
#   supervisor_error   our own bug — says nothing about the config, but is not
#                      transient either, so retrying just burns the night.
#   health_check       the service came up and then answered wrongly; usually
#                      the config, but observed often enough for unrelated
#                      reasons (a wedged tokenizer worker) that condemning the
#                      parameters would be guessing.
#   bench_preflight    the benchmark platform could not reach the endpoint.
#   terminated         a clean SIGTERM: a stop, a window cutoff, a redeploy.
#   released           we took the run back (pause); see RELEASED above.
#   unknown            by definition unclassified.


def classify_failure(log_text: str) -> str:
    """Cheap keyword-based failure classification. An LLM triage pass can
    replace/extend this later — the interface is just log text in, class out."""
    lowered = log_text.lower()
    rules: list[tuple[str, tuple[str, ...]]] = [
        ("oom", ("cuda out of memory", "outofmemoryerror", "out of memory")),
        ("nccl", ("nccl", "communicator")),
        # Before port_conflict: the GPU-busy message is worded to avoid the
        # "already in use" needle so the two never cross-classify.
        ("gpu_busy", ("gpu busy on", "gpus busy on")),
        ("port_conflict", ("address already in use", "already in use on")),
        (
            "image_missing",
            (
                "unable to find image",
                "failed to resolve reference",
                "pull access denied",
                "manifest unknown",
            ),
        ),
        ("bad_config", ("unrecognized arguments", "invalid argument", "error: argument")),
        ("model_load", ("no such file", "not a valid model", "safetensors")),
        ("ssh_timeout", ("timed out after",)),
        ("image_pull", ("pulling from", "manifest for", "denied")),
    ]
    for failure_class, needles in rules:
        if any(needle in lowered for needle in needles):
            return failure_class
    return "unknown"


def classify_exit(exit_code: int | None, oom_killed: bool) -> str | None:
    """Classify from container exit status when the logs said nothing."""
    if oom_killed:
        return "oom"  # host/cgroup OOM-killer
    if exit_code == 137:
        return "killed_externally"  # SIGKILL: docker kill, a human, or the OOM reaper
    if exit_code == 143:
        return "terminated"  # SIGTERM
    if exit_code == 139:
        return "segfault"
    return None


def is_infrastructure(failure_class: str) -> bool:
    """Retry this elsewhere; do not hold it against the configuration."""
    return failure_class in INFRASTRUCTURE_FAILURES


def is_transient_placement(failure_class: str) -> bool:
    """Still queueing, not yet wedged. Keep waiting rather than failing the run."""
    return failure_class in TRANSIENT_PLACEMENT_FAILURES


def is_config_induced(failure_class: str) -> bool:
    """A verdict on the parameters. Feed it back as an infeasible point rather
    than dropping it — a config that OOMs is information, and a searcher that
    never hears about it will propose the same region again."""
    return failure_class in CONFIG_FAILURES
