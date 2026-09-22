from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.hardware import coerce_gpu_type

# -- auth ---------------------------------------------------------------------


class RegisterRequest(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=6, max_length=128)


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


def _stripped_or_blank(value: object) -> object:
    """Trim, so a whitespace-only value fails `min_length` instead of being
    saved as the empty string the DELETE endpoint exists to write."""
    return value.strip() if isinstance(value, str) else value


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    role: str
    created_at: datetime | None = None
    # The name this user's submissions are published under on the benchmark
    # platform. Not a secret — returned in full.
    contributor: str = ""


class ContributorSet(BaseModel):
    """Save the name your submissions are published under.

    Shown on the benchmark platform's submission list. A display name you own
    — the platform never treats it as an identity, and it is copied onto each
    submission when it is made, so changing it later never rewrites one
    already in flight.
    """

    contributor: str = Field(min_length=1, max_length=255)

    _strip = field_validator("contributor", mode="before")(lambda v: _stripped_or_blank(v))


class PasswordChange(BaseModel):
    # The current one is required even though the caller is already
    # authenticated: a token left behind on a shared machine should not be
    # enough to lock its owner out of their own account.
    current_password: str
    new_password: str = Field(min_length=6, max_length=128)


class UserRoleUpdate(BaseModel):
    role: str  # admin | user


# -- machines -----------------------------------------------------------------


class MachineCreate(BaseModel):
    name: str
    host: str
    ssh_user: str = "root"
    ssh_port: int = 22
    gpu_count: int = 8
    gpu_type: str = ""
    notes: str = ""
    # "" = platform default (ssh_docker today). Set "k8s" for a machine that is
    # really a slice of the GPU cluster, reached through the k8s API rather than
    # ssh. The fleet is mixed during the migration, so it is set per machine.
    driver: str = ""
    # k8s only: which `clusters` row this slice lands in. None = the platform
    # default cluster (the AUTOTUNE_K8S_* env), which is what every machine
    # meant before clusters existed.
    cluster_id: int | None = None
    # k8s only: which nodes this slice's pods may land on, as "label=value"
    # pairs (e.g. "kubernetes.io/hostname=gpu-005"). Empty = scheduler is free.
    node_selector: str = ""
    # Where the ENGINE reaches this box for inter-node traffic (dist-init /
    # NCCL), when that is not the same as `host` — `host` is the ssh target and
    # the address LLMBench is given. Empty = `host`. Only worth setting for a
    # box in a node group whose rail address differs from its management one.
    data_host: str = ""
    # NCCL_SOCKET_IFNAME, when the routable IP and the IB rail differ. Empty =
    # the engine/driver default. Usually set on the GROUP (as `nccl_env`), not
    # per machine; here for a member whose interface alone differs.
    nccl_ifname: str = ""

    @field_validator("gpu_type")
    @classmethod
    def _canonical_gpu_type(cls, v: str) -> str:
        # A controlled vocabulary, not free text: a typo'd card type is one no
        # baseline compares against. Legacy/product strings normalize; unknowns
        # 422. For k8s machines the cluster probe overrides this anyway.
        return coerce_gpu_type(v)


class MachineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    host: str
    ssh_user: str
    ssh_port: int
    gpu_count: int
    gpu_type: str
    driver: str
    cluster_id: int | None = None
    # Resolved from the clusters table for display; empty = the default cluster.
    cluster_name: str = ""
    node_selector: str
    data_host: str = ""
    nccl_ifname: str = ""
    state: str
    notes: str
    baseline: dict[str, Any]
    baseline_status: str
    # Cards currently held by live runs — how much of the machine is actually
    # in use, which `state` alone cannot say once runs share a machine.
    gpus_busy: int = 0
    # Which node group this machine belongs to, and its rank in it (0 = master).
    # Empty / None = not grouped, which is the ordinary single-node machine.
    # Membership is a topology, not a reservation: a grouped machine is still
    # schedulable for single-node work (see the group endpoints).
    group: str = ""
    group_rank: int | None = None
    # The lease, so the UI can tell "ours for the night" from "ours but
    # leaving" — a distinction `state` cannot carry, since both are AVAILABLE.
    lease_state: str = "none"
    lease_holder: str = ""
    lease_note: str = ""
    leased_at: datetime | None = None
    lease_due_at: datetime | None = None
    lease_end_mode: str = ""
    lease_deadline_at: datetime | None = None
    lease_released_at: datetime | None = None


class MachineStateUpdate(BaseModel):
    state: str  # away | available


# -- node groups --------------------------------------------------------------
#
# A node group is the multi-node resource: leased machines the operator has
# declared can be deployed as one gang, with a master. It is deliberately NOT a
# lease and NOT a reservation — the machines stay in the single-node fleet, and
# a member is taken only while a gang is live on it.


class MachineGroupCreate(BaseModel):
    name: str
    # Member machine names, in RANK ORDER: index 0 is the master (rank 0), and
    # the rest are workers 1..N-1 in the order the driver launches them. Names
    # rather than ids on purpose — every other pin in the platform (a campaign's
    # `machine_names`, the lease API) speaks names, and the caller knows those.
    members: list[str] = Field(..., min_length=1)
    # "" = each member's own driver. Set it to reject a mixed group early; a
    # single deployment cannot span substrates.
    driver: str = ""
    # The interconnect's NCCL knobs (NCCL_SOCKET_IFNAME, NCCL_IB_HCA, …),
    # applied to every rank's container. One fact about one fabric.
    nccl_env: dict[str, str] = Field(default_factory=dict)
    # 0 = the platform picks a free rendezvous port per run on the master.
    dist_port: int = Field(0, ge=0, le=65535)
    extra_env: dict[str, str] = Field(default_factory=dict)
    extra_volumes: dict[str, str] = Field(default_factory=dict)
    notes: str = ""


class MachineGroupUpdate(BaseModel):
    """The same fields, all optional: a PATCH-shaped edit sent as a PUT so the
    form can save a subset without erasing the rest."""

    members: list[str] | None = None
    driver: str | None = None
    nccl_env: dict[str, str] | None = None
    dist_port: int | None = Field(None, ge=0, le=65535)
    extra_env: dict[str, str] | None = None
    extra_volumes: dict[str, str] | None = None
    notes: str | None = None


class MachineGroupMemberOut(BaseModel):
    name: str
    rank: int
    is_master: bool
    host: str
    data_host: str = ""
    gpu_count: int
    gpu_type: str
    driver: str
    # Whether this member is ours right now and usable, so the page can show a
    # group as ready-to-deploy or blocked without a second request per member.
    leased: bool
    state: str
    baseline_status: str
    needs_attention: bool
    gpus_busy: int = 0
    # When this member's lease is promised back. Shown on the group because a
    # multi-node run is cut short by whichever member goes first.
    lease_due_at: datetime | None = None


class MachineGroupOut(BaseModel):
    id: int
    name: str
    driver: str
    # Which cluster the gang lives in; None = the platform-default cluster.
    cluster_id: int | None = None
    nccl_env: dict[str, Any]
    dist_port: int
    extra_env: dict[str, Any]
    extra_volumes: dict[str, Any]
    notes: str
    members: list[MachineGroupMemberOut]
    # Nodes the group deploys. Equal to `len(members)`; carried explicitly
    # because it is the number every form and validator actually reasons about.
    node_count: int
    # True when every member is leased, usable and cleared — i.e. the group
    # could be deployed right now. Advisory: the scheduler re-checks at launch.
    deployable: bool
    # One line per reason it is not deployable ("node-26 is not leased"), so the
    # page can say what to fix instead of just disabling a button.
    blockers: list[str] = []
    # Advisory and non-blocking: a member's lease expires soon, so a gang that
    # started now could be cut short when that member is handed back.
    warnings: list[str] = []
    created_at: datetime | None = None


# -- campaigns ----------------------------------------------------------------


class CampaignCreate(BaseModel):
    name: str
    engine: str  # sglang | vllm
    image: str
    model_path: str
    served_model_name: str
    search_space: dict[str, Any]
    objective: dict[str, Any] = Field(default_factory=dict)
    benchmark_slug: str = ""  # "" = platform default
    service_port: int = 28200  # avoid 30000-32767 (k8s NodePort range)
    extra_env: dict[str, str] = Field(default_factory=dict)
    extra_volumes: dict[str, str] = Field(default_factory=dict)  # host -> container[:ro]
    run_baseline_canary: bool = True
    # Several runs of this campaign may occupy one machine at once, each pinned
    # to its own cards. Off gives a run the whole machine.
    share_machine: bool = True
    machine_names: list[str] = Field(default_factory=list)  # empty = any machine
    # A node group each run deploys ACROSS, by name. Empty = single-node and
    # `machine_names` is the whole pin. Set = the group's members are the pin
    # and their count is the deployment width; the config's world size
    # (tp×dp×pp) must divide evenly across them. sglang only for now.
    node_group: str = ""
    # A one-off window. Leave both null and give `daily_start`/`daily_end`
    # instead for a campaign that repeats; the supervisor then writes these two
    # itself, one occurrence at a time.
    window_start: datetime | None = None
    window_end: datetime | None = None
    # The recurring window, as local clock times: "23:00" to "08:00" every
    # night in `schedule_timezone`, until `schedule_until`.
    daily_start: str = ""
    daily_end: str = ""
    schedule_timezone: str = ""
    schedule_until: datetime | None = None
    max_run_minutes: int = 150
    # The external search container for this campaign: when set, it decides
    # what to try and the platform stops enumerating the space itself
    # (docs/api/policy-contract.md). `policy_settings` is validated against
    # schemas.policy.PolicySettings at save.
    policy_id: int | None = None
    policy_settings: dict[str, Any] = Field(default_factory=dict)
    # Re-run the best N candidates before the campaign finishes, so a winner
    # is backed by more than one measurement. 0 = off.
    confirm_top_k: int = 0
    confirm_repeats: int = 3
    # The expensive second stage: after screening, re-measure the best N
    # against this benchmark and let that decide. Both fields, or neither.
    verify_benchmark_slug: str = ""
    verify_top_k: int = 0
    verify_objective: dict[str, Any] = Field(default_factory=dict)
    verify_max_run_minutes: int = 180
    # The LLMBench collection profile the replay stage measures against. Empty
    # = whatever the benchmark resolves at submit time, which is fine for a
    # campaign that finishes in one night and wrong for one that does not.
    dataset_profile: str = ""
    dataset_policy: str = "rebuild_at_start"
    # Which release branch of the deploy repo this campaign's winner is
    # proposed onto. The repo keeps one per (model x card x engine), so it is a
    # choice — empty means the branch the bound baseline already tracks.
    deploy_branch: str = ""
    # Open that merge request the moment the campaign is done, without waiting
    # for someone to press Generate MR. Honours the platform's dry-run setting
    # exactly as the button does.
    auto_promote: bool = False


class MachineWarningOut(BaseModel):
    """A row-only finding about a machine a run is pinned to: unavailable (not
    leased), expiring (lease ends before the run would), or expired (past due).
    Shown as a banner before launch, alongside the deeper ssh preflight."""

    machine: str  # "" for a fleet-wide finding (no pool pinned, nothing leased)
    status: str   # unavailable | expiring | expired
    detail: str


class CampaignOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    owner_id: int
    name: str
    engine: str
    image: str
    model_path: str
    served_model_name: str
    search_space: dict[str, Any]
    objective: dict[str, Any]
    status: str
    benchmark_slug: str
    service_port: int
    extra_env: dict[str, Any]
    extra_volumes: dict[str, Any]
    run_baseline_canary: bool
    share_machine: bool
    machine_names: list[str]
    node_group: str = ""
    window_start: datetime | None
    window_end: datetime | None
    daily_start: str
    daily_end: str
    schedule_timezone: str
    schedule_until: datetime | None
    override_until: datetime | None
    max_run_minutes: int
    policy_id: int | None
    policy_settings: dict[str, Any]
    confirm_top_k: int
    confirm_repeats: int
    verify_benchmark_slug: str
    verify_top_k: int
    verify_objective: dict[str, Any]
    verify_max_run_minutes: int
    dataset_profile: str
    dataset_policy: str
    deploy_branch: str = ""
    auto_promote: bool = False
    # What the campaign is actually measuring against, once it has started:
    # the build id, its hash, and whether that was a fresh build or one it
    # adopted from a campaign already using it.
    dataset_build_id: str
    dataset_sha256: str
    dataset_policy_applied: str
    dataset_pinned_at: datetime | None
    # What the search space expands to, declared rather than planned: the
    # Candidate rows only exist once the campaign is active, so a
    # campaign that has not started yet has none and would otherwise read as
    # an empty sweep.
    candidate_count: int
    created_at: datetime
    # Row-only lease findings about this campaign's pinned machines, computed at
    # read time (never an ORM column). Empty when every pinned machine is leased
    # with enough runway.
    machine_warnings: list[MachineWarningOut] = Field(default_factory=list)


class CampaignStatusUpdate(BaseModel):
    # scheduled hands control back to the clock; paused takes it away. draft is
    # not settable — a campaign that has started is no longer a draft.
    status: str  # active | paused | scheduled


class CampaignScheduleUpdate(BaseModel):
    """Edit the clock without touching anything else about the campaign."""

    daily_start: str = ""
    daily_end: str = ""
    schedule_timezone: str = ""
    schedule_until: datetime | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None


# -- runs / results -----------------------------------------------------------


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    campaign_id: int
    candidate_id: int
    machine_id: int | None
    kind: str
    # "screen" (the cheap benchmark, every candidate) or "verify" (the
    # expensive one, the shortlist). Read off the run's candidate, so every
    # query that serializes this must eager-load it.
    stage: str
    status: str
    gpu_indices: list[int]
    service_port: int
    container_name: str
    endpoint_url: str
    launch_command: str
    llmbench_submission_id: str
    failure_class: str
    error: str
    # Set when a policy session drove this run (a launch or its validation), so
    # the UI can offer that session's container log alongside the run's.
    policy_session_id: int | None = None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class RunDetailOut(RunOut):
    config: dict[str, Any]
    results: list["ResultOut"]
    # What the run actually executed on (image digest, engine/torch/CUDA
    # versions, GPU model). Detail view only — it is provenance you go looking
    # for, not something to carry on every row of a list.
    env_snapshot: dict[str, Any] = Field(default_factory=dict)


class ResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    source: str
    passed: bool
    score: float | None
    metrics: dict[str, Any]
    # The campaign objective resolved against these metrics when they landed.
    objective_value: float | None = None
    feasible: bool = False
    breaches: list[str] = Field(default_factory=list)
    created_at: datetime


class LeaderboardEntry(BaseModel):
    run_id: int
    config: dict[str, Any]
    score: float | None
    metrics: dict[str, Any]
    holds_redlines: bool = True
    breaches: list[str] = Field(default_factory=list)
    # "screen" or "verify". The two stages measure different things with
    # different benchmarks, so their scores share no scale — a reader that
    # merges them into one ordering is comparing tok/min against value/min.
    stage: str = "screen"
    # Which metric this entry's score IS, so a table can label its own column
    # rather than assume the campaign's screening target.
    target_metric: str = ""
    # False when the run replayed a different dataset build than the campaign
    # pinned. The measurement is real; it just cannot be ranked against the
    # others, so it sorts to the bottom and says why rather than disappearing.
    comparable: bool = True
    dataset_build_id: str = ""
    # This entry's score ÷ the same-stage production baseline measured this
    # campaign. Read with the objective's direction: on a maximize target > 1
    # beats production. None when no baseline ran this stage. Because the
    # baseline rides the same dataset each night, the ratio stays comparable
    # across nights even as absolute numbers drift — which is why the board
    # ranks on it whenever it exists.
    vs_baseline: float | None = None
    # This row IS the production baseline, not a candidate.
    is_baseline: bool = False
    # The canonical GPU type this run landed on (e.g. "H100"), from the node it
    # actually ran on. Empty when the substrate could not say. A run on a
    # different card than the stage's baseline is not normalized against it —
    # per-card throughput is card-count normalized, not card-type normalized —
    # so it carries no vs_baseline and ranks below the same-chip entries.
    card_type: str = ""


class RedlineIn(BaseModel):
    metric: str
    op: str = "<="
    value: float


class ObjectiveCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    target_metric: str
    direction: str | None = None  # None -> inferred from the metric
    redlines: list[RedlineIn] = Field(default_factory=list)


class ObjectiveOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    owner_id: int | None
    name: str
    description: str
    target_metric: str
    direction: str
    redlines: list[dict[str, Any]]
    is_builtin: bool
    created_at: datetime
    updated_at: datetime


class SearchSpaceDraft(BaseModel):
    """A space being edited. Nameless on purpose: the editor asks what a space
    expands to on every keystroke, long before anyone has named it."""

    engine: str = "sglang"
    base: dict[str, Any] = Field(default_factory=dict)
    grid: dict[str, list[Any]] = Field(default_factory=dict)
    # Parameters swept together instead of crossed: each group is
    # {param: [values]}, columns of equal length, zipped row by row.
    tied: list[dict[str, list[Any]]] = Field(default_factory=list)
    # Parameters swept over an interval: {param: {"min", "max", "step"}}.
    # Serialized under "range" to match the search-space JSON the expander
    # reads; the column it lands in is `ranges`.
    range: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # {param: {gating_param: [values]}} — the parameter is dropped from a
    # candidate whose gating values do not match.
    conditions: dict[str, dict[str, Any]] = Field(default_factory=dict)


class SearchSpaceCreate(SearchSpaceDraft):
    name: str = Field(min_length=1, max_length=128)
    description: str = ""


class SearchSpaceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    owner_id: int
    name: str
    engine: str
    description: str
    base: dict[str, Any]
    grid: dict[str, list[Any]]
    tied: list[dict[str, list[Any]]]
    range: dict[str, dict[str, Any]] = Field(
        default_factory=dict, validation_alias="ranges"
    )
    conditions: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # Computed from the space itself, so the list, the editor and the platform
    # cannot disagree about how big a night this is.
    candidate_count: int
    created_at: datetime
    updated_at: datetime


class SearchSpacePreview(BaseModel):
    candidate_count: int
    invalid: list[dict[str, Any]]
    gpu_count_used: int
    sample: list[dict[str, Any]]
    # Mistakes in the space itself (a ragged tied group, a duplicated
    # parameter) — these block saving, unlike `invalid`.
    errors: list[str] = Field(default_factory=list)


class CandidateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    config: dict[str, Any]
    status: str
    validation_error: str
    cards: int = 0  # GPUs this config occupies (tp × dp × pp)


# -- baselines ----------------------------------------------------------------


class BaselineIn(BaseModel):
    """Create or update a production reference config — a whole LaunchConfig
    keyed by (served model, engine, card type).

    The config can be given field by field, or a raw production `command`
    (a whole `docker run …` line or a bare serve command) supplied and
    compiled server-side — the "paste what production runs" path. Explicit
    fields win over the paste.
    """

    served_model_name: str = Field(min_length=1, max_length=128)
    engine: str = "sglang"
    card_type: str = ""
    engine_args: dict[str, Any] = Field(default_factory=dict)
    image: str = ""
    model_path: str = ""
    service_port: int = 0
    extra_env: dict[str, str] = Field(default_factory=dict)
    extra_volumes: dict[str, str] = Field(default_factory=dict)
    weights_fingerprint: str = ""
    command: str = ""
    notes: str = ""

    @field_validator("card_type")
    @classmethod
    def _canonical_card_type(cls, v: str) -> str:
        # Same vocabulary as a machine's gpu_type: a baseline is keyed by it, so
        # "A100" and "A100-SXM4-80GB" must not be two different references.
        return coerce_gpu_type(v)


class BaselineImportIn(BaseModel):
    """Create a baseline FROM its deploy-repo file: the identity, plus where
    the file lives. The config comes from the file, never from the body."""

    served_model_name: str = Field(min_length=1, max_length=128)
    engine: str = "sglang"
    card_type: str = ""
    project: str = Field(min_length=1)
    branch: str = Field(min_length=1)
    path: str = "config/model.yaml"
    format: dict[str, Any] = Field(default_factory=dict)
    policy: dict[str, Any] = Field(default_factory=dict)
    notes: str = ""

    @field_validator("card_type")
    @classmethod
    def _canonical_card_type(cls, v: str) -> str:
        return coerce_gpu_type(v)


class DeployBindingIn(BaseModel):
    """Bind (or re-bind) a baseline to its deploy-repo file. `format` is
    `{preset}` or `{adapter, options}`; empty = the default preset."""

    project: str = Field(min_length=1)
    branch: str = Field(min_length=1)
    path: str = "config/model.yaml"
    format: dict[str, Any] = Field(default_factory=dict)
    policy: dict[str, Any] | None = None
    equivalences: dict[str, Any] | None = None


class DeployBindingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    baseline_id: int
    project: str
    branch: str
    path: str
    format: dict[str, Any]
    policy: dict[str, Any]
    equivalences: dict[str, Any]
    divergences: list[dict[str, Any]]
    unresolved: int = 0
    commit: str
    synced_at: datetime | None = None
    updated_at: datetime | None = None


class BaselineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    served_model_name: str
    engine: str
    card_type: str
    engine_args: dict[str, Any]
    image: str = ""
    model_path: str = ""
    service_port: int = 0
    extra_env: dict[str, Any] = Field(default_factory=dict)
    extra_volumes: dict[str, Any] = Field(default_factory=dict)
    weights_fingerprint: str = ""
    # GPUs the config occupies, derived the same way a candidate's are.
    cards: int = 0
    source: str
    notes: str
    binding: DeployBindingOut | None = None
    created_at: datetime
    updated_at: datetime | None = None


class BaselineSyncOut(BaseModel):
    """What a sync from the deploy repo did to the row."""

    baseline: BaselineOut
    commit: str
    # Platform-owned differences taken into the row, and everything else
    # found — each with its status (unresolved, or how it was decided).
    adopted: list[dict[str, Any]] = Field(default_factory=list)
    divergences: list[dict[str, Any]] = Field(default_factory=list)
    unresolved: int = 0
    # Facts read off the file.
    image: str = ""
    model_path: str = ""
    gpus: int | None = None
    gpu_product: str = ""
    env: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class DivergenceResolveIn(BaseModel):
    kind: str = Field(pattern="^(field|knob)$")
    key: str = Field(min_length=1)
    action: str = Field(pattern="^(adopt|equivalent|repo|ignore|platform)$")


class BindingPolicyIn(BaseModel):
    """One policy edit: a shortcut by name, or one field/knob's owner."""

    shortcut: str = ""
    field: str = ""
    knob: str = ""
    owner: str = ""


class EquivalenceIn(BaseModel):
    field: str = ""
    knob: str = ""
    ours: str
    theirs: str


# -- promotions ---------------------------------------------------------------


class PromoteRequest(BaseModel):
    """Roll a campaign winner onto the cluster.

    `run_id` omitted means "the current top of the leaderboard". `target` empty
    means the platform default (manual). `force` promotes a run that crossed a
    redline anyway — a deliberate override, not the normal path.
    """

    run_id: int | None = None
    target: str = ""
    notes: str = ""
    force: bool = False
    # gitlab target only: the release branch to open the merge request against,
    # overriding the campaign's own and the binding's. Set once here and the
    # campaign remembers it, so the next winner goes to the same place.
    branch: str = ""
    # gitlab target only: also delete production knobs the winner's config
    # never mentions. Off by default — absence in a search space is not a
    # decision, and the preview lists what would go.
    apply_removals: bool = False
    # gitlab target only: fields to treat as platform-owned for THIS merge
    # request ("image", "model_path", "gpus") — the "also update image.tag"
    # tick, without changing the binding's policy.
    promote_fields: list[str] = Field(default_factory=list)


class SubmissionPromoteRequest(BaseModel):
    """Propose a measured Baseline Hub submission to production."""

    target: str = ""
    notes: str = ""
    branch: str = ""
    apply_removals: bool = False
    promote_fields: list[str] = Field(default_factory=list)


class DeployBranchIn(BaseModel):
    """Where this campaign's winner goes, and whether it
    goes by itself.

    `branch` empty clears it, back to the bound baseline's branch.
    `auto_promote` omitted leaves the setting alone, so moving a branch does
    not quietly arm (or disarm) unattended promotion.
    """

    branch: str = ""
    auto_promote: bool | None = None


class MergeRequestPreview(BaseModel):
    """What `promote` would send to GitLab, before it does.

    Everything an operator needs to decide: which file on which branch, whether
    the platform's copy of it is stale, the knob-level change list, what is
    deliberately NOT applied, and the exact diff. `ready` is false when there
    is nothing to change or the baseline is not bound to a repo.
    """

    campaign_id: int
    run_id: int
    baseline_id: int | None = None
    ready: bool
    reason: str = ""
    repo_project: str = ""
    # Where the merge request goes, and the branch the baseline is synced
    # against. They differ when a campaign names a branch of its own.
    repo_branch: str = ""
    tracked_branch: str = ""
    repo_path: str = ""
    head_commit: str = ""
    synced_commit: str = ""
    stale: bool = False
    # Nothing to propose: the winner is what production already runs.
    unchanged: bool = False
    unresolved: int = 0
    # Knob changes between the file at the synced commit and at head — what
    # moved in production since the platform last looked.
    drift: list[dict[str, Any]] = Field(default_factory=list)
    plan: dict[str, Any] = Field(default_factory=dict)
    diff: str = ""
    source_branch: str = ""
    title: str = ""
    description: str = ""
    dry_run: bool = True
    offline: bool = False
    evidence: dict[str, Any] = Field(default_factory=dict)


class PromotionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    campaign_id: int
    run_id: int
    target: str
    state: str
    config: dict[str, Any]
    refs: dict[str, Any]
    detail: str
    error: str
    created_by: int | None = None
    created_at: datetime
    updated_at: datetime | None = None
