"""Core tables. Enum values are stored as plain strings (native str enums) so
migrations stay trivial while states are still typed in code."""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.control.search.space import candidate_count
from app.db.base import Base

# Portable JSON: JSONB on Postgres, JSON elsewhere (tests can use sqlite).
JsonCol = JSON().with_variant(JSONB(), "postgresql")


class UserRole(StrEnum):
    ADMIN = "admin"
    USER = "user"


class MachineState(StrEnum):
    AWAY = "away"            # in production, not ours tonight
    AVAILABLE = "available"  # borrowed to the platform
    RESERVED = "reserved"    # held by a run


class LeaseState(StrEnum):
    """Who has the right to spend this machine's time.

    The hand-over used to be a single boolean dressed as `state`, which could
    say "ours" or "not ours" and nothing in between. An external fleet manager
    needs a third answer — *giving it back, but not yet* — because taking a
    machine away from a 20-minute benchmark either wastes the run or waits for
    it, and which of those happens has to be the caller's choice.

        NONE     never leased, or the lease was released
        ACTIVE   ours; new runs may start
        DRAINING ours; finish what is running, start nothing new
        RELEASED handed back; production restored
    """

    NONE = "none"
    ACTIVE = "active"
    DRAINING = "draining"
    RELEASED = "released"


class LeaseEndMode(StrEnum):
    """How much of the current work survives the hand-back request.

    POLITE waits for the runs already in flight — up to `max_run_minutes` each,
    so it is not instant. EAGER stops them now and loses their measurements;
    it exists because sometimes the machine is needed more than the data is.
    """

    POLITE = "polite"
    EAGER = "eager"


class BaselineStatus(StrEnum):
    """Lifecycle of the handed-over production services on a borrowed machine.

    NONE -> CAPTURED (services + restore scripts recorded)
         -> CLEARED  (production stopped; the machine is ours to experiment on)
         -> RESTORED (production brought back and verified before hand-back)

    Experiments may only run while CLEARED: capture is what makes teardown
    reversible, so we never destroy something we cannot put back.
    """

    NONE = "none"
    CAPTURED = "captured"
    CLEARED = "cleared"
    RESTORED = "restored"


class RunKind(StrEnum):
    EXPERIMENT = "experiment"
    BASELINE = "baseline"  # canary against the handed-over production service
    # An engine the platform launched FOR a policy container ("delegated
    # launch"). Launched and health-gated like an experiment, but then held at
    # SERVING for the policy to use instead of being benchmarked and torn down.
    POLICY_LAUNCH = "policy_launch"
    # A benchmark of an endpoint the platform did NOT launch — an engine a
    # policy serves itself, or a contender served for validation. Advances like
    # a baseline canary (starts at the health gate, never launches, never tears
    # down); unlike a baseline it IS a search measurement and ranks.
    EXTERNAL = "external"


# Legacy marker. Before the baseline config was unified with search configs, a
# canary's "config" named a production container (`{"__baseline__": <name>}`)
# instead of carrying engine arguments. New canaries carry production's real
# engine args and are identified by `Candidate.kind`; this key is kept only so
# rows written by the old scheme are still recognised.
BASELINE_CONFIG_KEY = "__baseline__"


def is_baseline_config(config: dict | None) -> bool:
    return BASELINE_CONFIG_KEY in (config or {})


class CampaignStatus(StrEnum):
    """
        DRAFT      written, never started, no schedule
        SCHEDULED  will wake by itself when its window opens
        ACTIVE     inside its window, spending machine time
        PAUSED     held by a human; the schedule does not override this
        DONE       search exhausted, or the schedule has no occurrences left

    SCHEDULED and PAUSED both mean "not running now" and are deliberately
    different: one is waiting for a clock, the other for a person. Collapsing
    them is how a paused campaign silently restarts itself at 23:00.
    """

    DRAFT = "draft"
    SCHEDULED = "scheduled"
    ACTIVE = "active"
    PAUSED = "paused"
    DONE = "done"


class CandidateStatus(StrEnum):
    PENDING = "pending"
    VALID = "valid"
    INVALID = "invalid"
    EXHAUSTED = "exhausted"  # run(s) finished, terminal


class CandidateKind(StrEnum):
    SEARCH = "search"          # a point of the declared space
    CONFIRMATION = "confirmation"  # a repeat of an earlier candidate
    # A repeat of an earlier candidate against the EXPENSIVE benchmark: the
    # second stage of a staged campaign. Distinct from CONFIRMATION because it
    # is not more of the same evidence — it is different evidence, reported
    # under different metric names and judged by a different objective.
    VERIFICATION = "verification"
    # A canary measuring the handed-over production service. Carried on a
    # candidate row like the others, but its config is production's real engine
    # args (parsed from the captured command), so it reads and compares exactly
    # like a search config — it is simply never launched (baseline runs start
    # at the health gate against the already-deployed service).
    BASELINE = "baseline"


def is_baseline_candidate(candidate) -> bool:
    """The production reference, identified by its flag — with fallbacks to the
    legacy BASELINE kind and the older sentinel config, for rows written before
    baseline-ness was decoupled from kind."""
    return (
        bool(getattr(candidate, "is_baseline", False))
        or getattr(candidate, "kind", "") == CandidateKind.BASELINE.value
        or is_baseline_config(getattr(candidate, "config", None))
    )


def is_in_place_baseline(candidate) -> bool:
    """A baseline measured against the already-deployed production service: its
    run starts at the health gate and never launches a container, so it occupies
    no cards. The relaunch baseline — the reference measured by launching it like
    any candidate — is not one of these, and does occupy cards."""
    return (
        getattr(candidate, "kind", "") == CandidateKind.BASELINE.value
        or is_baseline_config(getattr(candidate, "config", None))
    )


class RunStatus(StrEnum):
    PENDING = "pending"
    LAUNCHING = "launching"
    WAITING_READY = "waiting_ready"
    HEALTH_CHECK = "health_check"
    BENCHING = "benching"
    # A delegated launch held up for its policy: healthy, occupying its cards,
    # doing whatever the policy sends it. Live (not terminal) — it ends when the
    # policy releases it or the session does.
    SERVING = "serving"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    KILLED = "killed"


TERMINAL_RUN_STATES = {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.KILLED}


class PolicySessionStatus(StrEnum):
    """One policy container's night on one machine.

        PENDING    decided, waiting for a cleared machine
        STARTING   container launched, waiting for its first heartbeat
        SEARCHING  the policy explores; trials and contenders stream in
        FINALIZING told to stop; waiting for it to wind down its engines
        VALIDATING contenders served one at a time and measured by the platform
        DONE       verdicts recorded, container gone

    FAILED is the platform's judgment (container died, policy wedged), ABORTED
    a human's. Both still validate registered contenders by fallback re-launch
    when the clock allows: a policy crashing at 04:00 loses its process, not
    its night.
    """

    PENDING = "pending"
    STARTING = "starting"
    SEARCHING = "searching"
    FINALIZING = "finalizing"
    VALIDATING = "validating"
    DONE = "done"
    FAILED = "failed"
    ABORTED = "aborted"


TERMINAL_SESSION_STATES = {
    PolicySessionStatus.DONE,
    PolicySessionStatus.FAILED,
    PolicySessionStatus.ABORTED,
}


class ContenderStatus(StrEnum):
    """A contender's path from claim to verdict.

    REGISTERED is a claim ("this is my rank-2 best"); VALIDATED/FAILED is the
    platform's answer. SUPERSEDED rows are kept, not deleted, when a later
    `PUT /contenders` replaces the list — they are the record of what the
    policy believed during the night. SKIPPED carries a reason (out of clock,
    unlaunchable spec) so a missing verdict is never silent.
    """

    REGISTERED = "registered"
    SUPERSEDED = "superseded"
    SERVING = "serving"
    VALIDATED = "validated"
    FAILED = "failed"
    SKIPPED = "skipped"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(16), default=UserRole.USER.value)
    # The name this user's submissions are published under on the benchmark
    # platform's own list. Not a secret and not an identity the platform
    # checks: a display name the user owns, saved here so it is typed once
    # rather than per submission. Empty = fall back to the username.
    contributor: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Cluster(Base):
    """A Kubernetes GPU cluster the platform may launch onto.

    Before this table the k8s driver was one cluster per worker: every
    `AUTOTUNE_K8S_*` value lived on `Settings`, a process-wide constant. A
    machine now binds to a cluster (`machines.cluster_id`), and this row holds
    exactly the values that differ between clusters — kubeconfig, namespace,
    workload kind, endpoint host, shm, runtime class. `AUTOTUNE_K8S_*` stays the
    **default** cluster's values, so a machine that names no cluster behaves
    exactly as it did before this table existed.

    The kubeconfig is stored **encrypted at rest** (`kubeconfig_enc`), keyed off
    the deployment's JWT secret, the same way every reversible secret is
    (`app.core.secrets`): it has to be replayed to the apiserver on every call,
    so it cannot be hashed, and a credential for a cluster the platform must
    never touch (an admin kubeconfig) is what a scoped one replaces. The API
    never returns the ciphertext — `ClusterOut` exposes `has_kubeconfig`.
    """

    __tablename__ = "clusters"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    # "client" (the Python client; the normal path), "kubectl", or "unavailable".
    api_mode: Mapped[str] = mapped_column(String(16), default="client")
    # The scoped kubeconfig, Fernet-encrypted. Empty = the ambient/default
    # credential (only sensible for the default cluster).
    kubeconfig_enc: Mapped[str] = mapped_column(Text, default="")
    context: Mapped[str] = mapped_column(String(128), default="")
    namespace: Mapped[str] = mapped_column(String(128), default="autotune")
    # "deployment" (the driver manages Deployment + NodePort Service) or
    # "custom" (a TuningRun the operator reconciles). Per cluster because an
    # operator may be installed on one cluster and not another.
    workload_kind: Mapped[str] = mapped_column(String(24), default="deployment")
    # The host LLMBench is given for a run's NodePort endpoint. Empty = derive
    # from a node InternalIP, which needs the cluster-scoped `nodes` read.
    node_host: Mapped[str] = mapped_column(String(255), default="")
    gpu_resource: Mapped[str] = mapped_column(String(64), default="nvidia.com/gpu")
    runtime_class: Mapped[str] = mapped_column(String(64), default="nvidia")
    tolerate_gpu_taint: Mapped[bool] = mapped_column(Boolean, default=True)
    tolerations: Mapped[str] = mapped_column(String(512), default="")
    image_pull_secrets: Mapped[str] = mapped_column(String(512), default="")
    model_pvc: Mapped[str] = mapped_column(String(255), default="")
    model_pvc_root: Mapped[str] = mapped_column(String(255), default="")
    shm_size_mb: Mapped[int] = mapped_column(Integer, default=2048)
    node_selector: Mapped[str] = mapped_column(String(512), default="")
    service_nodeport: Mapped[int] = mapped_column(Integer, default=0)
    run_ttl_seconds: Mapped[int] = mapped_column(Integer, default=0)
    in_cluster: Mapped[bool] = mapped_column(Boolean, default=False)
    cr_group: Mapped[str] = mapped_column(String(128), default="tuning.llm-autotune.io")
    cr_version: Mapped[str] = mapped_column(String(32), default="v1alpha1")
    cr_kind: Mapped[str] = mapped_column(String(64), default="TuningRun")
    cr_plural: Mapped[str] = mapped_column(String(64), default="tuningruns")
    cr_pod_label: Mapped[str] = mapped_column(String(128), default="tuning.llm-autotune.io/run")
    # Engine container requests/limits. Empty = omitted (today's behavior); a
    # quota'd namespace or a cluster standard that demands them fills these in.
    engine_cpu_request: Mapped[str] = mapped_column(String(32), default="")
    engine_memory_request: Mapped[str] = mapped_column(String(32), default="")
    engine_cpu_limit: Mapped[str] = mapped_column(String(32), default="")
    engine_memory_limit: Mapped[str] = mapped_column(String(32), default="")
    # Result of the last connection/capability probe, for the Resources page.
    last_probe: Mapped[dict] = mapped_column(JsonCol, default=dict)
    last_probe_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Machine(Base):
    __tablename__ = "machines"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    host: Mapped[str] = mapped_column(String(255))
    ssh_user: Mapped[str] = mapped_column(String(64), default="root")
    ssh_port: Mapped[int] = mapped_column(Integer, default=22)
    gpu_count: Mapped[int] = mapped_column(Integer, default=8)
    gpu_type: Mapped[str] = mapped_column(String(64), default="")
    # Deployment substrate: "ssh_docker" (bare metal we ssh into) or "k8s" (a
    # slice of the GPU cluster). Empty = the platform default driver. The fleet
    # is heterogeneous through the k8s migration, so this is per machine rather
    # than a global setting. See app/control/launch/base.py:MachineInfo.driver.
    driver: Mapped[str] = mapped_column(String(24), default="")
    # k8s only: which `clusters` row this slice belongs to. NULL = the platform
    # default cluster, i.e. today's global AUTOTUNE_K8S_* settings — which is
    # what every machine registered before clusters existed means, so the
    # migration rewrites no intent and a single-cluster deployment never notices
    # this column. Placement (node_selector) is per machine; the cluster is the
    # apiserver/namespace/workload-kind it is placed in.
    cluster_id: Mapped[int | None] = mapped_column(
        ForeignKey("clusters.id"), nullable=True, index=True
    )
    # k8s only: constrains which cluster nodes this slice's pods may land on, as
    # comma-separated `label=value` pairs (e.g. "kubernetes.io/hostname=gpu-a100-1"
    # or "nvidia.com/gpu.product=NVIDIA-A100-SXM4-80GB"). Empty = the scheduler
    # is free. This is what makes a k8s "machine" a named node-slice rather than
    # the whole cluster; weights served as a per-node hostPath live on only some
    # nodes, so without it the scheduler can place a pod where the model is
    # absent. Falls back to the global AUTOTUNE_K8S_NODE_SELECTOR when empty.
    node_selector: Mapped[str] = mapped_column(String(512), default="")
    # -- the interior address -------------------------------------------------
    #
    # `host` is the ssh target AND the address LLMBench is given. A multi-node
    # deployment adds a third use — where the engine listens for its peers — and
    # on a real box that is often a different NIC (an IB/RoCE rail) from the
    # management address. Empty = `host`, so every machine registered before
    # multi-node keeps working with no migration of intent.
    data_host: Mapped[str] = mapped_column(String(255), default="")
    # Interface name for NCCL when the routable IP and the rail differ, rendered
    # as NCCL_SOCKET_IFNAME. Empty = the engine/driver default.
    nccl_ifname: Mapped[str] = mapped_column(String(64), default="")
    state: Mapped[str] = mapped_column(String(16), default=MachineState.AWAY.value, index=True)
    # Quarantine: a machine the platform could not clean (a container that would
    # not tear down, cards it can no longer account for). Held out of scheduling
    # until a human clears it, rather than silently reused and colliding with
    # whatever is still stuck on it. `state` says what the box is doing;
    # this says "do not trust it is clean".
    needs_attention: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    attention_reason: Mapped[str] = mapped_column(Text, default="")
    attention_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    notes: Mapped[str] = mapped_column(Text, default="")
    # What was running when the machine was handed to us, and how to put it
    # back: {"services": [{container, image, port, served_model_name,
    # endpoint_url, restore_script}], "captured_at": ...}
    baseline: Mapped[dict] = mapped_column(JsonCol, default=dict)
    baseline_status: Mapped[str] = mapped_column(
        String(16), default=BaselineStatus.NONE.value
    )

    # -- the lease: who handed this machine over, and until when --------------
    #
    # `state` says what the machine is doing; the lease says on whose authority.
    # They are separate because a machine can be AVAILABLE-and-draining: still
    # ours, still finishing a run, but promised back and refusing new work.
    lease_state: Mapped[str] = mapped_column(
        String(16), default=LeaseState.NONE.value, index=True
    )
    # Free-form identity of whoever lent it: an API key's name, or "ui:alice".
    lease_holder: Mapped[str] = mapped_column(String(128), default="")
    lease_note: Mapped[str] = mapped_column(Text, default="")
    leased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When we promised it back. Reaching it starts a polite drain on its own,
    # so a caller that forgets to end the lease still gets the machine returned.
    lease_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_end_mode: Mapped[str] = mapped_column(String(16), default="")
    # The point past which we stop being graceful: live runs are killed even
    # mid-benchmark. Set from the end request; None means "no hard stop".
    lease_deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MachineGroup(Base):
    """A named, ordered set of machines that can be deployed as ONE gang.

    The multi-node resource: the operator leases N boxes, groups them, and picks
    a master, once. A campaign or a Baseline Hub submission then pins the group
    by name and the platform knows the node count, the rank order and the
    interior network without re-deciding any of it at launch time.

    **A group is a topology, not a reservation.** Membership says which boxes
    *can* form a gang; it does not take them out of the single-node fleet. A
    member is occupied only while a gang is actually live on it — see
    app/control/run_nodes.py:machine_hosts_live_gang — so the same two boxes can
    run independent single-node work all evening and one 2-node deployment in
    between. Nothing here is a lock, and there is deliberately no "reserved"
    column to set and forget.
    """

    __tablename__ = "machine_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    # Substrate for every member. Empty = each machine's own `driver` column.
    # A single deployment cannot span substrates, so a group is either
    # homogeneous or refused (validated at create, not trusted at launch).
    driver: Mapped[str] = mapped_column(String(24), default="")
    # Which cluster the gang lives in. A gang's pods talk to each other over the
    # cluster network, so a group cannot span clusters any more than it can span
    # substrates — validated at create against its members, not trusted at launch.
    cluster_id: Mapped[int | None] = mapped_column(
        ForeignKey("clusters.id"), nullable=True, index=True
    )
    # How the members reach each other: the interface and the NCCL knobs that
    # go with it. One fact about one interconnect, so it lives on the group
    # rather than being retyped per member.
    nccl_env: Mapped[dict] = mapped_column(JsonCol, default=dict)
    # 0 = the platform allocates a free rendezvous port per run on the master.
    # A fixed port collides the first time two runs share the master's host.
    dist_port: Mapped[int] = mapped_column(Integer, default=0)
    extra_env: Mapped[dict] = mapped_column(JsonCol, default=dict)
    extra_volumes: Mapped[dict] = mapped_column(JsonCol, default=dict)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    members: Mapped[list["MachineGroupMember"]] = relationship(
        back_populates="group", cascade="all, delete-orphan", order_by="MachineGroupMember.rank"
    )


class MachineGroupMember(Base):
    """One machine's place in a group: which group, and at which rank.

    Rank 0 is the master. The rank order is what the driver launches in, so it
    is stored rather than derived from insertion order — a set that reordered
    itself would produce a different gang every time it was re-read.
    """

    __tablename__ = "machine_group_members"

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("machine_groups.id"), index=True)
    machine_id: Mapped[int] = mapped_column(ForeignKey("machines.id"))
    rank: Mapped[int] = mapped_column(Integer, default=0)

    group: Mapped[MachineGroup] = relationship(back_populates="members")
    machine: Mapped[Machine] = relationship()

    __table_args__ = (
        UniqueConstraint("group_id", "rank"),
        # A box belongs to at most one group. Two groups sharing a member would
        # make "which gang owns this machine" ambiguous exactly when it matters
        # (a placement decision), and there is no use case that needs it.
        UniqueConstraint("machine_id"),
    )


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(128))
    engine: Mapped[str] = mapped_column(String(16))  # sglang | vllm
    image: Mapped[str] = mapped_column(String(255))
    model_path: Mapped[str] = mapped_column(String(255))
    served_model_name: Mapped[str] = mapped_column(String(128))
    # {"base": {fixed args}, "grid": {param: [values]}, "tied": [{param: [values]}]}
    search_space: Mapped[dict] = mapped_column(JsonCol)
    # {"target_metric": "...", "redlines": [{"metric": "...", "op": "<=", "value": x}]}
    objective: Mapped[dict] = mapped_column(JsonCol, default=dict)
    status: Mapped[str] = mapped_column(String(16), default=CampaignStatus.DRAFT.value, index=True)
    # Which LLMBench benchmark to run for this campaign ("" = platform default).
    benchmark_slug: Mapped[str] = mapped_column(String(100), default="")
    # Port the engine serves on. Default avoids 30000-32767: on k8s nodes
    # kube-proxy hijacks that NodePort range on the node IP, so an engine
    # there is unreachable from anywhere but localhost.
    service_port: Mapped[int] = mapped_column(Integer, default=28200)
    # Model-specific launch needs: extra env vars and bind mounts
    # (e.g. an MoE config dir, a patched protocol file). host_path ->
    # container_path, optionally suffixed ":ro".
    extra_env: Mapped[dict] = mapped_column(JsonCol, default=dict)
    extra_volumes: Mapped[dict] = mapped_column(JsonCol, default=dict)
    # Benchmark the handed-over production service before replacing it.
    run_baseline_canary: Mapped[bool] = mapped_column(Boolean, default=True)
    # May several of this campaign's runs occupy one machine at once, each
    # pinned to its own GPUs? An 8-card node running one tp=2 config wastes six
    # cards a night. Off means a run gets the whole machine, which is the
    # cleaner measurement when a config is sensitive to host-level contention.
    share_machine: Mapped[bool] = mapped_column(Boolean, default=True)
    # Machines this campaign may use (names). Empty = any available machine.
    # A campaign is tied to a model/image/hardware, so "any machine" is rarely
    # what you want once the fleet is heterogeneous.
    machine_names: Mapped[list] = mapped_column(JsonCol, default=list)
    # A node group to deploy each run across, by name. Empty = single-node, the
    # pin above is the whole story. When set, the group's members ARE the pin
    # and their count is the deployment width — the group is authoritative and
    # `machine_names` is left as it was, so unsetting the group restores the
    # previous single-node pin instead of silently losing it. Resolved through
    # app/control/orchestrator/lifecycle.py:machines_for.
    node_group: Mapped[str] = mapped_column(String(64), default="")
    # The occurrence currently in force. For a scheduled campaign these are
    # WRITTEN by the supervisor from `daily_start`/`daily_end` each time a
    # window opens; for an unscheduled one they are set by hand or left null.
    # Everything downstream — the run-fits check, the hand-back trigger, the
    # hard cutoff — reads only these two, and so never has to know whether the
    # campaign repeats.
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # -- the recurring schedule ----------------------------------------------
    #
    # "23:00" to "08:00" in `schedule_timezone`, every night until
    # `schedule_until`. Stored as local times rather than UTC instants because
    # 23:00 is a fact about the operator's night: converting it once at write
    # time would drift by an hour the next time the offset changes.
    daily_start: Mapped[str] = mapped_column(String(5), default="")   # "23:00"
    daily_end: Mapped[str] = mapped_column(String(5), default="")     # "08:00"
    schedule_timezone: Mapped[str] = mapped_column(String(64), default="")
    schedule_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # "Run now, whatever the clock says", until this instant. Set by Force
    # start. Without it, marking a scheduled campaign active is undone on the
    # very next tick, because the schedule sees no open window and puts it back
    # to sleep — the override is how a human outranks the clock for a while.
    # Bounded rather than open-ended: an override with no end is how a machine
    # stays borrowed for a week.
    override_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    max_run_minutes: Mapped[int] = mapped_column(Integer, default=150)
    # Set = this campaign's search runs in an external policy container: the
    # supervisor launches the image for the window and the container proposes
    # every config through the policy session API. Null = the platform simply
    # enumerates the declared search space, in order, once.
    policy_id: Mapped[int | None] = mapped_column(
        ForeignKey("policies.id"), nullable=True, index=True
    )
    # Per-campaign knobs for the policy session (validated by
    # schemas.policy.PolicySettings): max_contenders, approx_minutes_each,
    # validation_reserve_minutes, heartbeat_timeout_s. JSON rather than columns
    # because they only mean anything when policy_id is set.
    policy_settings: Mapped[dict] = mapped_column(JsonCol, default=dict)
    # Before crowning a winner, re-run the best candidates and see whether the
    # result holds. One benchmark is a signal, not a decision: measurement
    # noise on this rig is ~0.25%, but a config can also be fast on average and
    # occasionally miss a redline — which single-shot ranking cannot see at
    # all. 0 = off (the behaviour every campaign had before this existed).
    confirm_top_k: Mapped[int] = mapped_column(Integer, default=0)
    # Total measurements wanted per confirmed candidate, including the one it
    # already has. 3 is the smallest number that yields a spread rather than
    # just a second opinion.
    confirm_repeats: Mapped[int] = mapped_column(Integer, default=3)

    # -- the expensive second stage ------------------------------------------
    #
    # A guidellm sweep screens a candidate in minutes; a replay of real
    # production traffic measures it properly in the better part of an hour.
    # Running the second on every point of a grid spends a night on four
    # candidates, so the best `verify_top_k` earn it and the rest do not.
    #
    # Empty slug or top_k 0 means single-stage, which is what every campaign
    # written before this did.
    verify_benchmark_slug: Mapped[str] = mapped_column(String(100), default="")
    verify_top_k: Mapped[int] = mapped_column(Integer, default=0)
    # The second stage reports different metric names entirely, so it is judged
    # by its own objective. Empty = the platform default replay objective.
    verify_objective: Mapped[dict] = mapped_column(JsonCol, default=dict)
    # Time to reserve before the window closes for one verification run. Its
    # own number because it is several times a screening run.
    verify_max_run_minutes: Mapped[int] = mapped_column(Integer, default=180)

    # -- the dataset the expensive stage replays -------------------------------
    #
    # An LLMBench collection profile set aside for this platform, which nothing
    # rebuilds but us. Empty = take whatever the benchmark resolves, which is
    # what every campaign written before this did and is fine for a campaign
    # that finishes in one night.
    #
    # The build is pinned at the first measurement and kept for the campaign's
    # life: candidates are ranked against each other, so they have to be
    # measured with the same instrument. See app/datasets/pinning.py.
    dataset_profile: Mapped[str] = mapped_column(String(100), default="")
    dataset_policy: Mapped[str] = mapped_column(String(24), default="rebuild_at_start")
    dataset_build_id: Mapped[str] = mapped_column(String(64), default="")
    dataset_sha256: Mapped[str] = mapped_column(String(64), default="")
    dataset_pinned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # What actually happened — rebuilt, adopted, current — which is not always
    # what was asked for. An adopted pin is a decision someone should be able
    # to read back later, not a silent downgrade.
    dataset_policy_applied: Mapped[str] = mapped_column(String(32), default="")
    # The build row currently being waited on. Non-zero only between asking for
    # a build and it finishing; a worker restart resumes polling from here
    # rather than triggering a second one.
    dataset_build_row: Mapped[int] = mapped_column(Integer, default=0)

    # Open the winner's merge request by itself, the moment the campaign is
    # done, instead of waiting for someone to press Generate MR. Off by
    # default: a proposal that appears while nobody is watching is only welcome
    # when it was asked for. It honours the same dry-run setting as the button,
    # so an unarmed platform records the draft and writes nothing.
    auto_promote: Mapped[bool] = mapped_column(Boolean, default=False)
    # Which release branch of the deploy repo a winner of this campaign is
    # proposed onto. The repo keeps one branch per (model x card x engine), so
    # the same bound baseline can face several of them — a model tuned for the
    # next release line is not a change to the one in production. Empty = the
    # branch the baseline's binding tracks.
    deploy_branch: Mapped[str] = mapped_column(String(255), default="")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    @property
    def candidate_count(self) -> int:
        """What this campaign's space expands to — whether or not it has run.

        A space is only expanded once the campaign is ACTIVE, so a
        campaign that has not started yet has zero Candidate rows. Reporting
        THAT as the size of the search space told an operator their 48-config
        sweep was empty. This is the declared size; the planned rows are a
        separate, later fact.

        Imported inside the property because app.control.search.space is a
        layer above the model. Never raises: a malformed space is a finding for
        the validator, not a reason a campaign cannot be listed.
        """
        from app.control.search.space import candidate_count

        try:
            return candidate_count(self.search_space or {})
        except Exception:
            return 0

    owner: Mapped[User] = relationship()


class Candidate(Base):
    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    config: Mapped[dict] = mapped_column(JsonCol)
    config_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), default=CandidateStatus.PENDING.value)
    validation_error: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(16), default=CandidateKind.SEARCH.value)
    # The production reference, not a search point — measured on the same dataset
    # so candidates can be ranked as a multiple of it. Orthogonal to `kind`: a
    # baseline is screened (kind SEARCH) and verified (kind VERIFICATION) like any
    # config, so `stage_of` still reads its stage off the kind. Before this flag
    # a baseline WAS a kind, which is why it could never reach the verify stage.
    is_baseline: Mapped[bool] = mapped_column(Boolean, default=False)
    # A confirmation candidate deliberately carries the SAME config_hash as the
    # one it repeats — that hash is how a point is known to be tried already,
    # and a repeat must not read as new territory. This link is what
    # distinguishes "the same config again, on purpose" from a duplicate.
    repeat_of: Mapped[int | None] = mapped_column(
        ForeignKey("candidates.id"), nullable=True, index=True
    )
    # Where this config steps outside the campaign's declared search space
    # (unknown flag, off-grid value, …), as computed by search.space.deviations.
    # Informational, never a rejection: policies are allowed out of the space if
    # they declare it, and the leaderboard/report badge these rows. Null for
    # configs the platform enumerated itself, which cannot deviate by construction.
    deviations: Mapped[list | None] = mapped_column(JsonCol, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    campaign: Mapped[Campaign] = relationship()


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidates.id"), index=True)
    machine_id: Mapped[int | None] = mapped_column(ForeignKey("machines.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), default=RunKind.EXPERIMENT.value, index=True)
    status: Mapped[str] = mapped_column(String(24), default=RunStatus.PENDING.value, index=True)
    container_name: Mapped[str] = mapped_column(String(128), default="")
    endpoint_url: Mapped[str] = mapped_column(String(255), default="")
    # The exact cards and port this run owns. Stored, not derived: they are what
    # the launch spec must be rebuilt from when the worker re-attaches after a
    # restart, and what tells the scheduler which cards are still free.
    gpu_indices: Mapped[list] = mapped_column(JsonCol, default=list)
    service_port: Mapped[int] = mapped_column(Integer, default=0)
    launch_command: Mapped[str] = mapped_column(Text, default="")  # exact, for reproduction
    # What actually ran: image digest/id, engine + torch + CUDA versions, GPU
    # model. Read off the live container, because an image TAG is mutable —
    # two nights that both say "sglang:latest" can be different builds, and
    # without this the difference reads as measurement noise.
    env_snapshot: Mapped[dict] = mapped_column(JsonCol, default=dict)
    llmbench_submission_id: Mapped[str] = mapped_column(String(64), default="")
    failure_class: Mapped[str] = mapped_column(String(64), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    log_path: Mapped[str] = mapped_column(String(255), default="")
    # Set when a policy session asked for this run (delegated launch, external
    # bench, contender validation). The session view lists its runs by this,
    # and session teardown knows what to kill by this.
    policy_session_id: Mapped[int | None] = mapped_column(
        ForeignKey("policy_sessions.id"), nullable=True, index=True
    )
    # The caller's Idempotency-Key: a policy that retries a POST after a lost
    # response must get the run it already created, not a second engine on the
    # same cards. Unique per session (enforced in the router; sqlite tests have
    # no partial unique indexes), null for runs the platform decided on its own.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # A policy's DELETE /launches/{id} lands here; the worker turns it into a
    # teardown + SUCCEEDED on its next tick. Column rather than an Event so
    # GET /launches/{id} can honestly say "releasing".
    release_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # A terminal run whose container is not yet confirmed gone. The run is
    # finished for every other purpose (campaign completion, ranking), but its
    # cards and port stay reserved until the janitor sees the container
    # actually vanish — a slow or wedged teardown must not read as free cards
    # the next run then collides with. Cleared the moment teardown is
    # confirmed; the counter bounds how hard the janitor tries before it gives
    # up and quarantines the machine.
    teardown_pending: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    teardown_attempts: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When the cluster started making this run wait for cards, and NULL the rest
    # of the time. A queued run is not a slow one: on a shared GPU cluster the
    # scheduler holds the pod until production gives cards back, which is the
    # normal price of a lease that buys the right to ASK for a schedule rather
    # than a guarantee of one. Without this the wait is indistinguishable from a
    # stuck launch, and "3 hours in, still no GPU" is the single most useful
    # thing to be able to say about a campaign that has produced nothing.
    waiting_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # -- multi-node placement ------------------------------------------------
    #
    # The rendezvous port chosen on the MASTER for a multi-node deployment, so
    # every rank can be told the same `--dist-init-addr`. 0 for a single-node
    # run, which needs no rendezvous. See docs/findings_claude/multi-node-resources.md.
    dist_port: Mapped[int] = mapped_column(Integer, default=0)
    # The node group this run was placed from, by name. Empty for a run placed
    # on loose machines. Recorded rather than derived because a group can be
    # edited after the night, and "which topology produced this number" is
    # provenance that must not move under a result.
    node_group: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    campaign: Mapped[Campaign] = relationship()
    candidate: Mapped[Candidate] = relationship()
    machine: Mapped[Machine | None] = relationship()
    # Every run has at least one RunNode (rank 0, the master); a multi-node run
    # has one per machine. Kept in lockstep with the master columns above by the
    # mapper events at the bottom of this module, so `machine_id`/`gpu_indices`
    # keep meaning "the master node" for every reader that predates multi-node.
    run_nodes: Mapped[list["RunNode"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="RunNode.rank"
    )

    @property
    def stage(self) -> str:
        """"screen" or "verify" — which benchmark this run was measured by.

        Imported inside the property because app.staging imports this module;
        the alternative was to restate the rule here, and two definitions of
        which stage a run belongs to is precisely the drift that would send a
        run to one benchmark and score it against another's metric names.
        """
        from app.staging import stage_of

        return stage_of(self.candidate)


class RunNode(Base):
    """One machine's part of a run — the unit a multi-node deployment divides
    into.

    A run used to be one machine, and that fact lived in `runs.machine_id` /
    `gpu_indices` / `container_name` / `launch_command`. Multi-node makes those
    fields the MASTER's, and this table the answer to "which machines does this
    run occupy, with which cards, under which command". A single-node run has
    exactly one row (rank 0, `is_master`), so there is no second code path.

    Rank 0 is kept in lockstep with the run's master columns by the mapper
    events below: `RunNode` is the queryable truth, and the master columns are
    its projection onto the run row that every pre-multi-node reader still uses.
    """

    __tablename__ = "run_nodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    # Nullable for the same reason `runs.machine_id` is: removing a machine from
    # the fleet keeps the history and nulls the link, and the node's cards,
    # command and rank are the measurement's provenance, not the fleet's.
    machine_id: Mapped[int | None] = mapped_column(
        ForeignKey("machines.id"), nullable=True, index=True
    )
    # 0 = master. Dense and unique per run — the workers are ranks 1..N-1 in the
    # order the driver launches them, and that order is reproducible from here.
    rank: Mapped[int] = mapped_column(Integer, default=0)
    is_master: Mapped[bool] = mapped_column(Boolean, default=False)
    gpu_indices: Mapped[list] = mapped_column(JsonCol, default=list)
    service_port: Mapped[int] = mapped_column(Integer, default=0)
    container_name: Mapped[str] = mapped_column(String(128), default="")
    # Exact command for THIS node. The run's own `launch_command` is the
    # master's, so a single-node reproduction is unchanged; a gang is
    # reproducible node by node only from here.
    launch_command: Mapped[str] = mapped_column(Text, default="")
    endpoint_url: Mapped[str] = mapped_column(String(255), default="")
    # Per-node state, because a gang fails node by node: a worker that never
    # joined is a different story from a master that never came up.
    status: Mapped[str] = mapped_column(String(24), default=RunStatus.PENDING.value)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    run: Mapped[Run] = relationship(back_populates="run_nodes")
    machine: Mapped[Machine | None] = relationship()

    __table_args__ = (UniqueConstraint("run_id", "rank"),)


def _master_node_values(run: Run) -> dict:
    """The rank-0 node a run's own columns describe.

    One builder for both the insert and the update event below, so the two
    cannot drift into disagreeing about what "the master node" means.
    """
    return {
        "run_id": run.id,
        "machine_id": run.machine_id,
        "rank": 0,
        "is_master": True,
        "gpu_indices": list(run.gpu_indices or []),
        "service_port": run.service_port or 0,
        "container_name": run.container_name or "",
        "launch_command": run.launch_command or "",
        "endpoint_url": run.endpoint_url or "",
        "status": run.status,
    }


@event.listens_for(Run, "after_insert")
def _insert_master_run_node(mapper, connection, target: Run) -> None:
    """Every run gets its rank-0 node in the same transaction as the run.

    A mapper event rather than a helper each call site remembers to use: the
    invariant has to hold for the runs the API creates, the baseline canary, a
    policy's delegated launches and every test double, and one forgotten site
    would silently produce a run no machine query can see. Core rather than ORM
    so it works identically under the sync worker session and the async API
    session, and so it can never re-enter the unit of work it was called from.
    """
    connection.execute(RunNode.__table__.insert().values(**_master_node_values(target)))


@event.listens_for(Run, "after_update")
def _sync_master_run_node(mapper, connection, target: Run) -> None:
    """Keep rank 0 mirroring the run's master columns after any change.

    The columns that matter here are set in two steps — a classic run's
    `container_name` needs the row's id, and the endpoint only exists once the
    driver has launched — so the insert alone would leave the node's copy
    empty. Syncing on update costs one small UPDATE per run transition and buys
    the rule that the master node row is never stale.

    Scoped to rank 0 on purpose: a gang's workers have their own cards, command
    and status, and a run-level update must not stamp the master's over them.
    """
    values = _master_node_values(target)
    values.pop("run_id", None)
    connection.execute(
        RunNode.__table__.update()
        .where(RunNode.run_id == target.id, RunNode.rank == 0)
        .values(**values)
    )


class Result(Base):
    __tablename__ = "results"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    source: Mapped[str] = mapped_column(String(32))  # health | llmbench
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    metrics: Mapped[dict] = mapped_column(JsonCol, default=dict)
    raw: Mapped[dict] = mapped_column(JsonCol, default=dict)
    # The campaign objective applied to `metrics`, resolved when the result
    # landed rather than re-derived on every report render. NULL means "not
    # summarized" — a health result, or a row written before 010 — and readers
    # fall back to computing it, so old campaigns still rank correctly.
    objective_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    feasible: Mapped[bool] = mapped_column(Boolean, default=False)
    # Signed redline slacks, satisfied when <= 0 (the convention optimizers
    # expect), and the human-readable form of the ones that were crossed.
    constraints: Mapped[list] = mapped_column(JsonCol, default=list)
    breaches: Mapped[list] = mapped_column(JsonCol, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    run: Mapped[Run] = relationship()


class SearchSpace(Base):
    """A named, reusable search space — built once in the UI, used by many
    campaigns. Same shape the expander consumes:
    {"base": {...}, "grid": {...}, "tied": [...]}.
    """

    __tablename__ = "search_spaces"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(128), unique=True)
    engine: Mapped[str] = mapped_column(String(16))  # sglang | vllm
    description: Mapped[str] = mapped_column(Text, default="")
    base: Mapped[dict] = mapped_column(JsonCol, default=dict)
    grid: Mapped[dict] = mapped_column(JsonCol, default=dict)
    # Groups of parameters swept together rather than crossed: each group is
    # {param: [values]} with equal-length columns, zipped row by row.
    tied: Mapped[list] = mapped_column(JsonCol, default=list)
    # Parameters swept over an interval instead of a list:
    # {param: {"min": x, "max": y, "step": s}}. The step is mandatory — it is
    # what keeps the space countable, which the preview and the night estimate
    # both need.
    ranges: Mapped[dict] = mapped_column(JsonCol, default=dict)
    # Parameters that only apply for certain values of others:
    # {param: {gating_param: [values]}}. An inactive parameter is dropped
    # before the config is hashed, so configs that deploy identically are one
    # candidate rather than several.
    conditions: Mapped[dict] = mapped_column(JsonCol, default=dict)

    @property
    def candidate_count(self) -> int:
        """How many configs this space expands to — asked of the expander, not
        recomputed by each caller, so the list, the editor and the platform
        cannot quote different numbers for the same space."""
        return candidate_count(
            {
                "base": self.base,
                "grid": self.grid,
                "tied": self.tied,
                "range": self.ranges,
                "conditions": self.conditions,
            }
        )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    owner: Mapped[User] = relationship()


class Objective(Base):
    """A named, reusable objective: what to maximize (or minimize), and the
    redlines a config must stay inside to count at all."""

    __tablename__ = "objectives"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    target_metric: Mapped[str] = mapped_column(String(128))
    direction: Mapped[str] = mapped_column(String(16), default="maximize")
    # Limits a run must stay inside to be eligible at all, however fast it was:
    # [{"metric": ..., "op": "<=", "value": ...}]
    redlines: Mapped[list] = mapped_column(JsonCol, default=list)
    # Shipped with the platform: always available, not owned by anyone.
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Dataset(Base):
    """Skeleton — daily-dataset API shapes are not confirmed yet (poc-scope.md)."""

    __tablename__ = "datasets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    version: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(255), default="")
    meta: Mapped[dict] = mapped_column(JsonCol, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Baseline(Base):
    """A production reference config, keyed by what it serves and on what.

    Not per machine: which physical box production was handed over on says
    nothing about the config, which is a property of the (model, engine, card
    type) it runs. A campaign tuning that combination compares its candidates
    against this reference — relaunched and measured on the same dataset — so
    "×1.15 of production" keeps its meaning across nights even as the absolute
    numbers drift.

    Populated automatically when a machine is handed over (capture upserts one
    per served model), and editable by hand: a reference can exist before any
    hand-over, and a stale one can be corrected without waiting for a re-capture.
    """

    __tablename__ = "baselines"
    __table_args__ = (
        UniqueConstraint(
            "served_model_name", "engine", "card_type", name="uq_baseline_identity"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    served_model_name: Mapped[str] = mapped_column(String(128), index=True)
    engine: Mapped[str] = mapped_column(String(32), default="sglang")
    card_type: Mapped[str] = mapped_column(String(64), default="")
    # Production's engine args — the same shape a search config carries, so the
    # relaunch and the card-norm read it identically.
    engine_args: Mapped[dict] = mapped_column(JsonCol, default=dict)
    # Provenance: "capture:<machine>" when inventoried on hand-over, "manual"
    # when a user entered it, "capture:<machine>" when captured from a box,
    # "gitlab:<branch>@<sha>" when synced from the deploy repo.
    source: Mapped[str] = mapped_column(String(128), default="manual")
    notes: Mapped[str] = mapped_column(Text, default="")
    # -- the rest of the LaunchConfig ------------------------------------------
    # A baseline used to be the knobs alone, which made it a lossy copy of
    # everything else that carries a config (a hub submission, a campaign, the
    # deploy file). It is the full LaunchConfig now; the knobs are still what
    # the leaderboard and the relaunch read, the rest is what "tune from
    # this" and the merge-request path need.
    image: Mapped[str] = mapped_column(String(255), default="")
    model_path: Mapped[str] = mapped_column(String(255), default="")
    service_port: Mapped[int] = mapped_column(Integer, default=0)
    extra_env: Mapped[dict] = mapped_column(JsonCol, default=dict)
    extra_volumes: Mapped[dict] = mapped_column(JsonCol, default=dict)
    # sha256 over config.json + the safetensors index: two model paths that
    # carry the same fingerprint hold the same weights, whatever they are
    # called. Empty until something computed it.
    weights_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )

    binding: Mapped["DeployBinding | None"] = relationship(
        back_populates="baseline", uselist=False, cascade="all, delete-orphan"
    )


class DeployBinding(Base):
    """Where a baseline's production config lives in git, and the decisions
    that make the two comparable.

    Production is deployed from a GitLab repo, one release branch per
    (model × card × engine); the model developer's configuration is one file
    on it. A bound baseline mirrors that file. But the platform's copy and the
    file are allowed to differ in ways that are not configuration — a weights
    path spelled for our machines, a mirror image tag, a draft model mounted
    somewhere else — and none of that may be reconciled silently. So the
    binding carries, beside the address of the file:

      format        which adapter reads/edits the file, and its options
                    (where the args list is, how it is spelled, …)
      policy        who owns each field/knob: `platform` (proposed in merge
                    requests, adopted on sync), `repo` (never written, every
                    difference reported), `ignore` (never written, not
                    reported — an explicit decision)
      equivalences  value pairs that mean the same thing on both sides
                    (ours ↔ theirs) — translated in both directions
      divergences   the differences found at the last sync, each unresolved
                    or resolved with how, by whom, when

    `document` is the file text at the last sync: the diff base when a merge
    request is opened, and the offline fallback for a preview.
    """

    __tablename__ = "deploy_bindings"

    id: Mapped[int] = mapped_column(primary_key=True)
    baseline_id: Mapped[int] = mapped_column(
        ForeignKey("baselines.id", ondelete="CASCADE"), unique=True, index=True
    )
    project: Mapped[str] = mapped_column(String(255), default="")
    branch: Mapped[str] = mapped_column(String(255), default="")
    path: Mapped[str] = mapped_column(String(255), default="config/model.yaml")
    format: Mapped[dict] = mapped_column(JsonCol, default=dict)
    policy: Mapped[dict] = mapped_column(JsonCol, default=dict)
    equivalences: Mapped[dict] = mapped_column(JsonCol, default=dict)
    divergences: Mapped[list] = mapped_column(JsonCol, default=list)
    commit: Mapped[str] = mapped_column(String(64), default="")
    document: Mapped[str] = mapped_column(Text, default="")
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )

    baseline: Mapped[Baseline] = relationship(back_populates="binding")

    @property
    def unresolved(self) -> int:
        return sum(1 for d in (self.divergences or []) if d.get("status") == "unresolved")


class ApiKey(Base):
    """A credential for a machine, not a person.

    The fleet manager that hands us GPUs is a service: it has no browser to log
    in from and no way to notice a token expiring at 3am. It gets a long-lived
    key instead, minted by a user who stays responsible for it.

    Only the hash is stored. The plaintext is returned exactly once, at
    creation, and cannot be recovered afterwards — a key we could show again is
    a key an attacker with database access could show themselves. `prefix` is
    the non-secret head of the key, kept so the UI can name a key in a list and
    so lookup is an indexed hit rather than a scan-and-compare over every row.

    Hashed with SHA-256 rather than bcrypt on purpose: a key is 32 bytes of
    system randomness, so there is no low-entropy password to slow an attacker
    down, and this runs on every external request.
    """

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    prefix: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    key_hash: Mapped[str] = mapped_column(String(64))
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    # Reported in the audit trail as `key:<name>`, so a lease started by a
    # service is attributable without the key itself appearing in the log.
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Session tokens only. A key minted for a policy session is scoped to that
    # session (the policy router requires it; every other principal-taking
    # route refuses it) and dies shortly after the session does — a long-lived
    # unscoped key inside a third-party container would outlive the trust that
    # created it.
    policy_session_id: Mapped[int | None] = mapped_column(
        ForeignKey("policy_sessions.id"), nullable=True, index=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    owner: Mapped[User] = relationship()

    @property
    def active(self) -> bool:
        return self.revoked_at is None


class PromotionState(StrEnum):
    """Lifecycle of a winner's rollout. Mirrors
    app.control.promotion.base.PromotionState; the DB stores the plain string so
    a new target's states never need a migration."""

    DRAFT = "draft"
    SUBMITTED = "submitted"
    AB_TESTING = "ab_testing"
    ROLLED_OUT = "rolled_out"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Promotion(Base):
    """A campaign winner handed to CICD to roll onto the serving cluster.

    The record of a decision: which run's config was promoted, the exact payload
    that crossed the seam (config, so it is reproducible even if the run is later
    pruned), where the external system put it (refs — an MR url, a pipeline id),
    and how far the rollout got. One campaign can have several over its life
    (a re-tune supersedes an earlier winner), so this is not one-to-one.
    """

    __tablename__ = "promotions"

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    # Which PromotionTarget opened it ("manual", "gitlab", …).
    target: Mapped[str] = mapped_column(String(32), default="manual")
    state: Mapped[str] = mapped_column(
        String(24), default=PromotionState.DRAFT.value, index=True
    )
    # The exact config that was promoted — self-contained, so the rollout is
    # reproducible without re-deriving it from the run/candidate later.
    config: Mapped[dict] = mapped_column(JsonCol, default=dict)
    # External identifiers the target created and polls (mr_url, pipeline_id,
    # branch, artifact). Shape is target-specific.
    refs: Mapped[dict] = mapped_column(JsonCol, default=dict)
    detail: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    campaign: Mapped[Campaign] = relationship()
    run: Mapped[Run] = relationship()


class Event(Base):
    """Audit trail: user actions and orchestrator decisions."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor: Mapped[str] = mapped_column(String(64))  # username or "worker"
    kind: Mapped[str] = mapped_column(String(64), index=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"), nullable=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    payload: Mapped[dict] = mapped_column(JsonCol, default=dict)


# -- policy-as-code -----------------------------------------------------------
#
# A policy is an external container image holding a search algorithm; the
# platform launches it for a nightly window, serves it benchmarks on request,
# and validates its best contenders with the full replay. See
# docs/api/policy-contract.md — these tables are the platform side of that
# contract.


class Policy(Base):
    """A registered policy image: which container runs the search for a
    campaign that searches through a container instead of enumerating its space.

    The registry row is deliberately thin — name, image, provenance. Everything
    about HOW a night runs (contender count, timeouts) lives on the campaign in
    `policy_settings`, because the same image is expected to serve many
    campaigns with different budgets.
    """

    __tablename__ = "policies"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    image: Mapped[str] = mapped_column(String(255))
    repo_url: Mapped[str] = mapped_column(String(255), default="")
    version: Mapped[str] = mapped_column(String(64), default="")
    # Whether the container itself needs the GPUs mapped in (a self-serving
    # policy runs engines as subprocesses; a delegated-only one never touches a
    # card and can run without them).
    gpus_in_container: Mapped[bool] = mapped_column(Boolean, default=True)
    # Whether the container needs the campaign's model weights mounted at
    # /model. A delegated-only policy never reads them — it proposes configs and
    # asks the platform to launch engines — and on a cluster the mount is what
    # would drag an otherwise placement-free controller pod onto a node that
    # happens to hold the weights. Defaults true so every policy registered
    # before this field keeps the mount it was launched with.
    needs_model: Mapped[bool] = mapped_column(Boolean, default=True)
    # Static env for the container (e.g. an LLM API key for an agent policy).
    env: Mapped[dict] = mapped_column(JsonCol, default=dict)
    # How many host ports a session should reserve for it.
    ports: Mapped[int] = mapped_column(Integer, default=4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    owner: Mapped[User] = relationship()


class PolicySession(Base):
    """One policy container's night on one machine.

    `status` is owned by the worker — the API process never writes it. Routes
    record what the policy SAID (heartbeats, finalized_at, the plan) in their
    own columns, and the worker folds those into status transitions on its
    tick; two writers on one column is how a finalize and a timeout end up
    fighting over the same row.

    Deadlines are deliberately NOT columns: search/hard deadlines are computed
    from the campaign window and the machine lease on every read, because
    force-start rewrites the window and a frozen copy here would lie.
    """

    __tablename__ = "policy_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    policy_id: Mapped[int] = mapped_column(ForeignKey("policies.id"), index=True)
    machine_id: Mapped[int | None] = mapped_column(ForeignKey("machines.id"), nullable=True)
    # The window occurrence this session belongs to: one session per window,
    # and the next window makes a new one rather than resurrecting this row.
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), default=PolicySessionStatus.PENDING.value, index=True
    )
    gpu_indices: Mapped[list] = mapped_column(JsonCol, default=list)
    ports: Mapped[list] = mapped_column(JsonCol, default=list)
    container_name: Mapped[str] = mapped_column(String(128), default="")
    # Exact command, with the session token redacted before storing — this is
    # shown in the UI and must never leak the credential.
    launch_command: Mapped[str] = mapped_column(Text, default="")
    env_snapshot: Mapped[dict] = mapped_column(JsonCol, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # What the policy last said about itself (its words, not our judgment).
    policy_phase: Mapped[str] = mapped_column(String(32), default="")
    policy_message: Mapped[str] = mapped_column(Text, default="")
    policy_progress: Mapped[float | None] = mapped_column(Float, nullable=True)
    # The machine-read lifecycle signal from the heartbeat (contract v1.1):
    # "" working | "exhausted" | "error". Acted on, unlike the free-text phase.
    policy_status: Mapped[str] = mapped_column(String(16), default="")
    # The policy's own last coverage report — the self-reported supplement to
    # the platform-derived coverage (which is computed from the trial ledger).
    coverage: Mapped[dict] = mapped_column(JsonCol, default=dict)
    # Productivity watchdog bookkeeping. last_activity_at bumps on every
    # delegated launch/benchmark request; idle_strikes counts consecutive idle
    # windows (reset by any activity); last_idle_strike_at rate-limits striking
    # to at most once per idle timeout.
    last_activity_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    idle_strikes: Mapped[int] = mapped_column(Integer, default=0)
    last_idle_strike_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Why search ended, stamped when the session leaves SEARCHING: policy_exhausted
    # | policy_error | policy_idle | deadline | policy_wedged | container_died |
    # explicit. Observability — the UI and logs show the cause, not just "done".
    search_end_reason: Mapped[str] = mapped_column(String(32), default="")
    # First-heartbeat handshake: what this image can do. New heartbeat commands
    # are only sent to sessions whose capabilities include them.
    capabilities: Mapped[list] = mapped_column(JsonCol, default=list)
    contract_version: Mapped[str] = mapped_column(String(16), default="")
    sdk_version: Mapped[str] = mapped_column(String(32), default="")
    # The PUT /session/plan declaration: {"tuning": [...], "extras": [...],
    # "notes": ...}. The extras list is the allow-list for deviation badges.
    plan: Mapped[dict] = mapped_column(JsonCol, default=dict)
    finalize_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    abort_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The contender the heartbeat is currently commanding the policy to serve.
    serving_contender_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    failure_class: Mapped[str] = mapped_column(String(64), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    campaign: Mapped[Campaign] = relationship()
    policy: Mapped[Policy] = relationship()
    machine: Mapped[Machine | None] = relationship()

    @property
    def terminal(self) -> bool:
        return self.status in {s.value for s in TERMINAL_SESSION_STATES}


class PolicyTrial(Base):
    """One evaluation a policy reported (or the platform performed for it).

    The observability stream and the cross-night history — NOT the ranking
    ledger. Platform-measured trials point at their Run (which owns the
    authoritative Result); self-measured trials carry only what the policy
    claimed, and every reader shows them with a `self` provenance badge.
    """

    __tablename__ = "policy_trials"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("policy_sessions.id"), index=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    # Order within the session, assigned on insert; the UI streams by this.
    seq: Mapped[int] = mapped_column(Integer, default=0)
    config: Mapped[dict] = mapped_column(JsonCol, default=dict)
    config_hash: Mapped[str] = mapped_column(String(64), index=True, default="")
    source: Mapped[str] = mapped_column(String(16), default="self")  # self | platform
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    reported_metrics: Mapped[dict] = mapped_column(JsonCol, default=dict)
    reported_objective_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    reported_feasible: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    failure_class: Mapped[str] = mapped_column(String(64), default="")
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    session: Mapped[PolicySession] = relationship()


class PolicyContender(Base):
    """A policy's claim that one config is worth the full replay.

    `launch_spec` must be complete enough for the platform to launch it with
    the policy dead — that completeness is what makes the verdict promotable
    and the fallback possible. The link to Candidate/Run is filled when
    validation actually happens; until then the row is just the claim.
    """

    __tablename__ = "policy_contenders"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("policy_sessions.id"), index=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    rank: Mapped[int] = mapped_column(Integer, default=1)
    # {"engine_args": {...}, "image": "...", "env": {}, "volumes": {},
    #  "environment": {...}} — the declarative, reproducible spec.
    launch_spec: Mapped[dict] = mapped_column(JsonCol, default=dict)
    config_hash: Mapped[str] = mapped_column(String(64), index=True, default="")
    status: Mapped[str] = mapped_column(
        String(16), default=ContenderStatus.REGISTERED.value, index=True
    )
    # Why the policy believes in it: its own numbers, trial ids. Display only.
    evidence: Mapped[dict] = mapped_column(JsonCol, default=dict)
    trial_ids: Mapped[list] = mapped_column(JsonCol, default=list)
    deviations: Mapped[list | None] = mapped_column(JsonCol, nullable=True)
    candidate_id: Mapped[int | None] = mapped_column(ForeignKey("candidates.id"), nullable=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    served_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # When the heartbeat started commanding "serve this" — the reference clock
    # for "the policy never served it", which last_heartbeat_at cannot be (a
    # policy can heartbeat forever without serving).
    serve_commanded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    skip_reason: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    session: Mapped[PolicySession] = relationship()


class PolicyState(Base):
    """The opaque warm-start blob, per campaign (not per session): night 2
    reads what night 1 wrote. The platform never parses it; the 16 MB cap is
    enforced in the router."""

    __tablename__ = "policy_state"

    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), primary_key=True)
    blob: Mapped[bytes] = mapped_column(LargeBinary, default=b"")
    content_type: Mapped[str] = mapped_column(String(128), default="application/octet-stream")
    size: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AgentReport(Base):
    """A performance report an agent wrote from finished runs.

    The report is prose over a comparison the platform computed; both are
    kept. `comparison` is the ComparisonDocument as it was when the report was
    written, so the report stays readable after the benchmark is edited or a
    run is re-measured — the numbers in the prose and the numbers in the
    snapshot agree forever, whatever the live comparison says later.

    Assets (charts) ride inline as base64: a few PNGs per report, no second
    store to deploy, no path to break.
    """

    __tablename__ = "agent_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    baseline_run_id: Mapped[int] = mapped_column(Integer, index=True)
    attempt_run_ids: Mapped[list] = mapped_column(JsonCol, default=list)
    # The campaign these runs came from, when they share one. Only a filter
    # for the report list; a comparison may span campaigns, and then it is null.
    campaign_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    comparable: Mapped[bool] = mapped_column(Boolean, default=True)
    markdown: Mapped[str] = mapped_column(Text, default="")
    comparison: Mapped[dict] = mapped_column(JsonCol, default=dict)
    # [{name, content_type, data_base64}]
    assets: Mapped[list] = mapped_column(JsonCol, default=list)
    # Free-form provenance: {agent, model, template, …}
    generator: Mapped[dict] = mapped_column(JsonCol, default=dict)
    # "en" | "zh". The translations of one report point at the first one.
    lang: Mapped[str] = mapped_column(String(8), default="en")
    translation_of: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # Public names of the configs, baseline first; [] = Baseline / Optimized.
    labels: Mapped[list] = mapped_column(JsonCol, default=list)
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by_name: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
