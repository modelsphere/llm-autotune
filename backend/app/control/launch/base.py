"""Deployment driver interface (execution layer).

This is the k8s compatibility seam. A LaunchSpec is a declarative description of
one engine service; a driver renders it into whatever its substrate needs
(docker command over ssh today, pod spec later) and manages the lifecycle.

Driver contract:
- launch() returns fast (detached start); readiness is polled via state().
- state() must distinguish STARTING (still loading weights) from CRASHED.
- attach() re-finds a deployment after a worker restart — never relaunches.
- teardown() is idempotent and must succeed on half-dead deployments.
"""

import logging
from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class MachineInfo(BaseModel):
    name: str
    host: str
    ssh_user: str = "root"
    ssh_port: int = 22
    gpu_count: int = 8
    # The address the ENGINE uses to reach this machine for inter-node traffic
    # (dist-init / NCCL), when that is not the same as `host` — `host` is the
    # ssh target and the address LLMBench is given. Empty = `host`, which is
    # what every single-node machine means and what a box with one NIC gets.
    data_host: str = ""
    # Interface name for NCCL when the routable IP and the rail differ, rendered
    # as NCCL_SOCKET_IFNAME. Empty = the engine/driver default.
    nccl_ifname: str = ""
    # Which deployment substrate reaches this machine: "ssh_docker" (a
    # bare-metal box we hold at night) or "k8s" (a slice of the GPU cluster the
    # scheduler places pods on). Empty means "the platform default driver",
    # which is how every machine reads until the k8s migration labels one.
    # During the migration the fleet is heterogeneous, so the substrate is a
    # property of the machine, not a global switch.
    driver: str = ""
    # k8s only: which `clusters` row this machine's pods land in. None = the
    # platform-default cluster, i.e. the global AUTOTUNE_K8S_* settings — what
    # every machine meant before the clusters table existed, so a machine that
    # names no cluster is unchanged. The driver resolves it; MachineInfo itself
    # stays ORM-free and carries only the id.
    cluster_id: int | None = None
    # k8s only: the node-selector that defines this slice, as "label=value"
    # pairs. A property of the machine (which nodes it is), not a global — so
    # two k8s pools can pin to different nodes. Empty falls back to the global
    # AUTOTUNE_K8S_NODE_SELECTOR.
    node_selector: str = ""

    @property
    def interior_host(self) -> str:
        """Where the engine reaches this machine's peers — `data_host`, or the
        management address when the box has only one. The single place the
        fallback is applied, so a driver never has to remember it."""
        return self.data_host or self.host

    @classmethod
    def of(cls, machine) -> "MachineInfo":
        """Describe a DB `Machine` row as a driver sees it.

        Duck-typed on purpose: this is the substrate-facing module and it must
        not import the ORM. One factory rather than the same field list at eight
        call sites, because the list grows every time placement grows — the
        address columns would have been added in eight places and missed in one.
        """
        return cls(
            name=machine.name,
            host=machine.host,
            ssh_user=machine.ssh_user,
            ssh_port=machine.ssh_port,
            gpu_count=machine.gpu_count,
            data_host=getattr(machine, "data_host", "") or "",
            nccl_ifname=getattr(machine, "nccl_ifname", "") or "",
            driver=machine.driver,
            cluster_id=getattr(machine, "cluster_id", None),
            node_selector=machine.node_selector,
        )


def merge_volumes(defaults: dict[str, str], declared: dict[str, str]) -> dict[str, str]:
    """Platform default mounts plus the spec's own — one mount per container path.

    A declared volume WINS over a default that targets the same in-container
    path (mount options like an :ro suffix stripped): mounting your own
    prewarmed cache at /root/.cache is stated intent, and keeping both is a
    docker "Duplicate mount point" refusal at launch. A plain dict merge
    cannot see the collision — it keys by host path.
    """

    def target(path: str) -> str:
        return path.split(":", 1)[0].rstrip("/") or "/"

    taken = {target(path) for path in declared.values()}
    kept = {host: t for host, t in defaults.items() if target(t) not in taken}
    return {**kept, **declared}


class NodeAssignment(BaseModel):
    """One machine's part of a deployment: which box, which cards, which rank.

    The unit a multi-node launch divides into. `container_name` and `data_host`
    are carried per node because both are per machine: the container names must
    be distinct on a shared host and the rendezvous address is the master's — but
    each rank is told its own address as well.
    """

    machine: MachineInfo
    gpu_indices: list[int] = Field(default_factory=list)
    rank: int = 0  # 0 = master
    container_name: str = ""
    # This node's own interior address (its data_host, or host). Carried so a
    # per-node spec is self-describing rather than re-deriving the fallback.
    data_host: str = ""


class LaunchSpec(BaseModel):
    """Declarative config for one deployed engine service. Data, not strings."""

    run_id: int
    machine: MachineInfo
    engine: str  # sglang | vllm
    image: str
    model_path: str
    served_model_name: str
    engine_args: dict[str, Any] = Field(default_factory=dict)
    gpu_indices: list[int] = Field(default_factory=list)  # empty = all
    # NOT 30000-32767: kube-proxy hijacks that NodePort range on k8s nodes,
    # making the endpoint unreachable via the node IP (learned the hard way).
    port: int = 28200
    env: dict[str, str] = Field(default_factory=dict)
    volumes: dict[str, str] = Field(default_factory=dict)  # host_path -> container_path
    # -- the node plan --------------------------------------------------------
    #
    # Every machine this deployment spans, in rank order, master (rank 0) first.
    # A single-node run carries exactly one entry, so the driver has one shape to
    # handle; a caller that predates multi-node and builds no plan at all still
    # works because `launch_nodes` synthesizes the rank-0 entry from `machine`.
    nodes: list[NodeAssignment] = Field(default_factory=list)
    # THIS spec's own rank, set by `for_node`. 0 on the gang spec.
    rank: int = 0
    nnodes: int = 1
    # "<master interior host>:<dist_port>", handed to every rank. Empty for a
    # single-node deployment, which has no rendezvous.
    dist_init_addr: str = ""
    dist_port: int = 0
    # The container name for THIS node. Empty = the single-node default, which
    # keeps every run launched before multi-node byte-identical.
    node_container: str = ""

    @property
    def container_name(self) -> str:
        return self.node_container or f"autotune-run-{self.run_id}"

    @property
    def endpoint_url(self) -> str:
        return f"http://{self.machine.host}:{self.port}"

    @property
    def multi_node(self) -> bool:
        return self.nnodes > 1

    def for_node(self, node: NodeAssignment) -> "LaunchSpec":
        """A copy scoped to one node: its machine, cards, rank and name.

        `endpoint_url` follows the machine, so each rank's spec describes its own
        host; only the master's is ever recorded as the run's endpoint.
        `dist_init_addr` and `nnodes` are unchanged — the rendezvous is the same
        fact for every rank, and duplicated per node it would only drift.

        `nodes` is CLEARED: the result describes one node, and a driver that
        treated it as a plan would re-attach the whole gang every time it
        launched a single rank (and skip that rank's own GPU backstop).
        """
        return self.model_copy(
            update={
                "machine": node.machine,
                "gpu_indices": list(node.gpu_indices),
                "rank": node.rank,
                "nodes": [],
                "node_container": node.container_name
                or f"autotune-run-{self.run_id}"
                + ("" if node.rank == 0 else f"-r{node.rank}"),
            }
        )


class WorkloadSpec(BaseModel):
    """Declarative config for one generic container — a policy session's
    container, not an engine service.

    Deliberately NOT a LaunchSpec: no engine, no model args, no readiness
    endpoint. The command is the image's own business (empty = the image's
    entrypoint), readiness is whatever the caller decides (a policy is "ready"
    when it heartbeats, which no driver can see), and gpu_indices may be None
    for a container that never touches a card.
    """

    name: str  # container/workload name; the attach/teardown key
    machine: MachineInfo
    image: str
    command: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    # host_path -> container_path, ":ro" suffix on the container path respected.
    volumes: dict[str, str] = Field(default_factory=dict)
    gpu_indices: list[int] | None = None  # None = no GPUs at all
    ports: list[int] = Field(default_factory=list)  # host ports it may bind


class WorkloadState(StrEnum):
    RUNNING = "running"
    EXITED = "exited"
    GONE = "gone"


class DeploymentState(StrEnum):
    STARTING = "starting"  # container up, endpoint not ready (loading weights)
    READY = "ready"        # endpoint answering
    CRASHED = "crashed"    # container exited
    GONE = "gone"          # container not found


class DeploymentHandle(BaseModel):
    """Everything needed to find a deployment again — survives worker restarts
    because it is derived from the run row, not from in-memory state."""

    driver: str
    container_name: str
    machine: MachineInfo
    endpoint_url: str
    # Whether this handle names a generic WORKLOAD (a policy container) rather
    # than an engine service. The two are different objects on a cluster — an
    # engine is a TuningRun/Deployment that serves an endpoint, a policy is a
    # run-to-completion Job that serves nothing — so logs, teardown and
    # provenance have to address them differently. Substrates with one kind of
    # container (docker: everything is `docker run`) ignore it.
    workload: bool = False
    # The WORKERS of a multi-node deployment, in rank order; the handle itself is
    # the master (rank 0). Empty for single-node, which is every run whose spec
    # names no plan — so a driver that only special-cases `node_handles` still
    # does exactly what it did before.
    node_handles: list["DeploymentHandle"] = Field(default_factory=list)
    rank: int = 0

    @property
    def all_handles(self) -> list["DeploymentHandle"]:
        """The master plus every worker, rank order — the whole gang."""
        return [self, *self.node_handles]


class DeploymentDriver(ABC):
    name: str = "base"

    @abstractmethod
    def launch(self, spec: LaunchSpec) -> tuple[DeploymentHandle, str]:
        """Start the service detached. Returns (handle, exact_launch_command)."""

    # -- multi-node -----------------------------------------------------------

    def launch_nodes(self, spec: LaunchSpec) -> tuple[DeploymentHandle, list[str]]:
        """Start every node of a deployment, in rank order, and return the gang
        handle (its `node_handles` are the workers) plus each node's command in
        rank order.

        The default is a plain loop over `launch()`, master first, with a
        rendezvous gate between the master and the workers — which is the whole
        contract for any substrate whose nodes are independent containers
        (docker over ssh). A cluster that launches a gang as ONE object (k8s
        LeaderWorkerSet) overrides this and returns one handle with no workers.

        A node that fails to start tears the ALREADY-STARTED nodes down in
        reverse rank order before the error escapes: a master left running with
        no workers holds its cards, its port and its rendezvous socket, and is
        exactly the wedged half-deployment the teardown contract exists to
        prevent.
        """
        plan = spec.nodes or [self._single_node(spec)]
        ordered = sorted(plan, key=lambda node: node.rank)
        handles: list[DeploymentHandle] = []
        commands: list[str] = []
        try:
            for node in ordered:
                node_spec = spec.for_node(node)
                handle, command = self.launch(node_spec)
                handles.append(handle.model_copy(update={"rank": node.rank}))
                commands.append(command)
                if node.rank == 0 and len(ordered) > 1:
                    self.wait_rendezvous(node_spec)
        except Exception:
            for handle in reversed(handles):
                try:
                    self.teardown(handle)
                except Exception:
                    logger.exception(
                        "cleanup of %s after a failed node launch failed",
                        handle.container_name,
                    )
            raise
        master = handles[0]
        return master.model_copy(update={"node_handles": handles[1:]}), commands

    @staticmethod
    def _single_node(spec: LaunchSpec) -> NodeAssignment:
        """The plan a spec that names none implies: one node, rank 0."""
        return NodeAssignment(
            machine=spec.machine,
            gpu_indices=list(spec.gpu_indices),
            rank=0,
            container_name=spec.container_name,
            data_host=spec.machine.interior_host,
        )

    def wait_rendezvous(self, spec: LaunchSpec) -> None:
        """Block until the master can accept its workers, or give up loudly.

        Called with the MASTER's per-node spec, before the first worker starts.
        The default does nothing: a substrate whose gang is one object has no
        rendezvous for us to wait on, and a single-node deployment never calls
        this. A substrate with independent containers overrides it — starting a
        worker before the master's rendezvous socket is open is how a rank
        crashes on a race it should never have seen.
        """
        return None

    def unfinished(self, handle: DeploymentHandle) -> list[DeploymentHandle]:
        """The nodes of this deployment not yet confirmed gone.

        The janitor's give-up path uses it to quarantine the machine that is
        actually stuck, rather than the master of a gang whose worker is the
        thing that would not die. Default: any node `is_gone` says is still
        there, so a substrate that overrides `is_gone` gets this for free.
        """
        return [node for node in handle.all_handles if not self.is_gone(node)]

    @abstractmethod
    def state(self, handle: DeploymentHandle) -> DeploymentState: ...

    @abstractmethod
    def logs(self, handle: DeploymentHandle, tail: int = 200) -> str: ...

    @abstractmethod
    def teardown(self, handle: DeploymentHandle) -> None: ...

    @abstractmethod
    def attach(self, spec: LaunchSpec) -> DeploymentHandle | None:
        """Re-find an existing deployment for this spec (crash recovery).
        Returns None if nothing is running/known for it."""

    def request_stop(self, handle: DeploymentHandle) -> None:
        """Begin a graceful shutdown and return promptly — do NOT block until
        the container exits. A SIGTERM lets an engine release GPU memory and
        tear down NCCL cleanly, which frees the cards faster and more reliably
        than a kill; the forced removal, if the container ignores it, is the
        janitor's escalation via teardown(). Default: no graceful phase — fall
        straight to teardown(), so a substrate that cannot signal still makes
        progress (and confirms gone in the same pass)."""
        self.teardown(handle)

    def is_gone(self, handle: DeploymentHandle) -> bool:
        """Whether the container is confirmed absent — the signal the janitor
        waits for before it frees the cards. Default: derived from state(),
        which every driver already reports GONE for a missing container."""
        return self.state(handle) == DeploymentState.GONE

    def gpus_in_use(self, machine: MachineInfo, indices: list[int]) -> list[int]:
        """Which of `indices` are physically busy right now, read from the
        hardware rather than the platform's own run ledger — the launch-time
        backstop against a card a dying container still holds. Only substrates
        the worker can probe implement it; the default trusts the ledger
        (returns none busy), which is exactly today's behaviour. A cluster
        scheduler that owns allocation (k8s) has no need for it."""
        return []

    def exit_info(self, handle: DeploymentHandle) -> tuple[int | None, bool]:
        """(exit_code, oom_killed) for a crashed deployment, when the substrate
        knows. Used to classify failures that leave nothing in the logs
        (e.g. an external SIGKILL). Default: unknown."""
        return (None, False)

    def failure_reason(self, handle: DeploymentHandle) -> tuple[str, str] | None:
        """Why a run is wedged, as (failure_class, human message), when the
        substrate can say without a log — a pod that never started (bad mount,
        unpullable image, unschedulable) leaves nothing to classify from logs,
        but the substrate still knows why. Default: no reason available."""
        return None

    def startup_status(self, handle: DeploymentHandle) -> dict[str, Any]:
        """Where a coming-up deployment is in its startup, so the supervisor can
        tell "still pulling a cold image" from "container up, model loading" and
        give each its own timeout.

        Returns {"phase": "pulling"|"running"|"unknown", "running_since": <RFC3339
        str|None>}:
          - "pulling"  — the workload exists but no container is running yet
                         (scheduling, or pulling the image). The generous
                         image-pull budget applies.
          - "running"  — a container is running (loading the model); the tight
                         ready budget applies, measured from `running_since`.
          - "unknown"  — the substrate cannot tell (the default). The caller
                         keeps its single ready-timeout behaviour, unchanged.
        """
        return {"phase": "unknown", "running_since": None}

    def placement(self, handle: DeploymentHandle) -> str:
        """Has the substrate actually given this run its hardware yet?

        Distinct from `startup_status`, which asks how far along a run that
        HAS hardware is. This asks whether it has any:

          - "placed"  — the cards are ours (or no scheduler was ever involved)
          - "queued"  — asked for, not yet granted. The run is pure demand: it
                        occupies nothing and has done no work, so it can be
                        withdrawn and resubmitted at no cost.
          - "unknown" — cannot tell right now; callers treat it as "placed",
                        because acting on a guess would withdraw live work.

        Default "placed": a substrate with no scheduler decides at launch —
        `docker run` either started the container or it failed. Only a cluster
        can hold a request in a queue, so only a cluster overrides this.
        """
        return "placed"

    def environment(self, handle: DeploymentHandle) -> dict[str, Any]:
        """What actually ran, as opposed to what was asked for.

        A campaign records an image *tag*, and a tag is mutable: two nights a
        week apart can both say `sglang:latest` and mean different builds, so
        a result that cannot be reproduced looks like noise rather than a
        version change. The digest and the driver's own view of the container
        are what make a number re-checkable months later.

        Best-effort by contract — a run must never fail because its
        provenance could not be read. Default: nothing known.
        """
        return {}

    # -- generic workloads (policy containers) --------------------------------

    def launch_workload(self, spec: WorkloadSpec) -> tuple[DeploymentHandle, str]:
        """Start a generic container detached. Returns (handle, exact command).

        Unlike launch(), no readiness is implied: the caller decides what
        "up" means (a policy session is up when it heartbeats). Idempotent the
        same way launch() is — a leftover container with this name is removed
        first, so a worker that crashed between launching and committing can
        safely do it again.
        """
        raise NotImplementedError(f"{self.name} driver cannot launch workloads")

    def workload_state(self, handle: DeploymentHandle) -> WorkloadState:
        """Alive or not, with no endpoint probing — state() reads /v1/models,
        which a policy container does not serve, so it would report a healthy
        policy as STARTING forever."""
        raise NotImplementedError(f"{self.name} driver cannot inspect workloads")

    # -- baseline lifecycle --------------------------------------------------
    # The production services already on a borrowed machine. Capture must be
    # rich enough to restore, because clear() is destructive.

    @abstractmethod
    def capture_baseline(self, machine: MachineInfo) -> dict[str, Any]:
        """Record what is running and how to bring it back."""

    @abstractmethod
    def clear_baseline(self, machine: MachineInfo, baseline: dict[str, Any]) -> list[str]:
        """Stop the captured production services. Returns what was stopped."""

    @abstractmethod
    def restore_baseline(self, machine: MachineInfo, baseline: dict[str, Any]) -> list[str]:
        """Bring the production services back. Returns what was restored."""

    def verify_baseline(self, machine: MachineInfo, baseline: dict[str, Any]) -> list[dict]:
        """Compare the current deployment against the capture. Restoring is not
        the same as restoring faithfully — a restore script can disagree with
        the container it recreates. Default: no verification available."""
        return []

    # -- capacity ------------------------------------------------------------

    def probe_capacity(self, machine: MachineInfo) -> dict[str, Any]:
        """The GPU capacity and card type this machine really has, read from the
        substrate rather than typed by hand.

        Only substrates that can answer implement it — a k8s node-slice reads its
        node(s); a bare-metal box's cards are whatever the operator recorded. The
        default says "cannot probe", so a caller keeps the hand-entered values.
        Shape when supported: {"supported": True, "gpu_count": int,
        "gpu_type": str, "node_count": int, "nodes": [...], "warnings": [...]}.
        """
        return {"supported": False}
