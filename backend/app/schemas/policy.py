"""Wire shapes for policy-as-code (docs/api/policy-contract.md).

Two audiences share this module on purpose: the policy-facing router
(/api/policy/v1, consumed by third-party containers) and the human-facing
registry/session views. The contract doc is the narrative version of the
policy-facing half; a field added here must be additive there.

Contract evolution rule: policies ignore fields they do not know, and so do
we — every inbound model here tolerates extras rather than rejecting them,
because a newer SDK talking to an older platform must degrade, not 422.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

CONTRACT_VERSION = "1.1"

# Inbound payloads from policy containers: unknown fields are the future, not
# an error.
_lenient = ConfigDict(extra="ignore")


# -- campaign-side settings -----------------------------------------------------


class PolicySettings(BaseModel):
    """The validated shape of `campaigns.policy_settings`.

    Stored as JSON on the campaign because these knobs only mean anything when
    `policy_id` is set; parsed through this model everywhere they are read, so
    a typo'd key fails at campaign save rather than at 3am.
    """

    model_config = ConfigDict(extra="forbid")

    max_contenders: int = Field(default=2, ge=1, le=8)
    # The benchmark measurement per contender, NOT counting the cold model
    # load — that is `model_startup_minutes`, budgeted separately so a
    # slow-loading model does not get its validation cut off at the window
    # edge. What the deadline math reserves per contender and what the manifest
    # promises the policy.
    approx_minutes_each: int = Field(default=60, ge=5, le=240)
    # Cold-start budget: how long this model takes to load and become
    # serveable before a benchmark can run against it. Added on top of
    # `approx_minutes_each` for every contender in the validation reserve, so a
    # 15-minute-loading model reserves that time per contender instead of
    # discovering the shortfall at 3am. 0 keeps the old all-in behaviour.
    model_startup_minutes: int = Field(default=0, ge=0, le=120)
    # Overrides the derived default
    # (max_contenders * (approx_minutes_each + model_startup_minutes) + 15).
    validation_reserve_minutes: int | None = Field(default=None, ge=5, le=720)
    # Cards this session holds on its machine. None = the whole box, the
    # default for a night that has the machine to itself. Set (together with
    # `share_machine` on the campaign) to take only a slice — the cards the
    # widest candidate needs — so several sessions can share one node.
    cards: int | None = Field(default=None, ge=1, le=64)
    heartbeat_timeout_s: int | None = Field(default=None, ge=30, le=3600)
    # Productivity watchdog: a SEARCHING session with nothing running and no
    # delegated request for this long earns a strike; `search_idle_strikes`
    # consecutive strikes (any activity resets the count) declares the search
    # over and finalizes it — the enforceable backstop for a policy that
    # forgets to signal "exhausted" or livelocks while still heartbeating.
    search_idle_timeout_s: int | None = Field(default=None, ge=30, le=3600)
    search_idle_strikes: int = Field(default=3, ge=1, le=10)

    def reserve_minutes(self) -> int:
        if self.validation_reserve_minutes is not None:
            return self.validation_reserve_minutes
        per_contender = self.approx_minutes_each + self.model_startup_minutes
        return self.max_contenders * per_contender + 15


# -- registry (human-facing, JWT) -----------------------------------------------


class PolicyIn(BaseModel):
    name: str = Field(min_length=2, max_length=128)
    description: str = ""
    image: str = Field(min_length=1, max_length=255)
    repo_url: str = ""
    version: str = ""
    gpus_in_container: bool = True
    # Mount the campaign's weights at /model. False for a delegated-only policy:
    # it never reads them, and on k8s the mount would pin a placement-free
    # controller pod to a weights-bearing node.
    needs_model: bool = True
    env: dict[str, str] = Field(default_factory=dict)
    ports: int = Field(default=4, ge=1, le=16)


class PolicyOut(PolicyIn):
    model_config = ConfigDict(from_attributes=True)
    id: int
    owner_id: int
    created_at: datetime | None = None


# -- the manifest (policy-facing) -------------------------------------------------


class ManifestModel(BaseModel):
    engine: str
    image: str
    model_path: str
    served_model_name: str
    extra_env: dict[str, Any] = Field(default_factory=dict)
    extra_volumes: dict[str, Any] = Field(default_factory=dict)


class ManifestHardware(BaseModel):
    machine: str
    gpu_type: str = ""
    gpu_indices: list[int] = Field(default_factory=list)
    ports: list[int] = Field(default_factory=list)
    gpus_visible_in_container: bool = True


class ManifestObjective(BaseModel):
    target_metric: str
    direction: str
    redlines: list[dict[str, Any]] = Field(default_factory=list)


class ManifestTime(BaseModel):
    started_at: datetime | None
    search_deadline: datetime | None
    hard_deadline: datetime | None
    heartbeat_interval_s: int
    heartbeat_timeout_s: int
    tick_s: int


class ManifestContenders(BaseModel):
    max: int
    validation_suite: str = "verify"
    approx_minutes_each: int


class BenchmarkSuite(BaseModel):
    slug: str
    approx_minutes: int
    dataset_build_id: str = ""


class ManifestServices(BaseModel):
    launch: bool = True
    benchmarks: dict[str, BenchmarkSuite] = Field(default_factory=dict)
    state: bool = True


class SessionManifest(BaseModel):
    contract: dict[str, str] = Field(default_factory=lambda: {"version": CONTRACT_VERSION})
    session_id: int
    campaign_id: int
    policy: dict[str, str]
    model: ManifestModel
    hardware: ManifestHardware
    objective: dict[str, ManifestObjective]
    search_space: dict[str, Any]
    space_policy: dict[str, str] = Field(
        default_factory=lambda: {"deviations": "allowed_if_declared"}
    )
    production: dict[str, Any] = Field(default_factory=dict)
    time: ManifestTime
    contenders: ManifestContenders
    services: ManifestServices
    prior: dict[str, Any] = Field(default_factory=dict)


# -- heartbeat --------------------------------------------------------------------


class DimCoverage(BaseModel):
    """What a policy has explored along one declared axis.

    Keyed by the platform's own parameter name (`space.swept_keys()`), so the
    two sides speak one coordinate system. Discrete points go in `tried_values`;
    a continuous/model-based sweep reports the interval(s) it has covered in
    `tried_ranges` ([{"min": .., "max": ..}]). `skipped_values` is intent — a
    region the policy deliberately will not try (e.g. tp=1 OOMs) — so coverage
    shows it as "skipped", not "unexplored".
    """

    model_config = _lenient

    param: str
    tried_values: list[Any] = Field(default_factory=list)
    tried_ranges: list[dict[str, Any]] = Field(default_factory=list)
    skipped_values: list[Any] = Field(default_factory=list)


class PolicyCoverage(BaseModel):
    """The policy's own account of how far it has searched — the *supplement*
    the platform cannot derive from the trial ledger: a completion estimate for
    an infinite/model-based space, deliberate skips, and self-served work the
    platform never saw as a trial. Advisory: the platform trusts its own
    ledger for what was actually measured and badges this as self-reported.
    """

    model_config = _lenient

    fraction: float | None = Field(default=None, ge=0.0, le=1.0)
    dimensions: list[DimCoverage] = Field(default_factory=list)
    note: str = ""


class HeartbeatIn(BaseModel):
    model_config = _lenient

    phase: str = ""
    message: str = ""
    progress: float | None = Field(default=None, ge=0.0, le=1.0)
    # Explicit, machine-read lifecycle signal (contract v1.1). "" == still
    # working; "exhausted" == search space done, finalize me; "error" == the
    # policy broke internally and is giving up. The platform acts on these
    # (graceful early finalize) rather than waiting out the deadline, but never
    # depends on them — the deadline, the liveness timeout and the idle
    # watchdog all still govern a policy that never sends one.
    status: str = ""
    failure_class: str = ""  # accompanies status == "error"
    coverage: PolicyCoverage | None = None
    # First-beat handshake.
    sdk_version: str | None = None
    contract_version: str | None = None
    capabilities: list[str] | None = None


class HeartbeatOut(BaseModel):
    command: str  # run | finalize | serve | exit | abort
    search_deadline: datetime | None
    hard_deadline: datetime | None
    contender_id: int | None = None
    port: int | None = None


# -- plan ---------------------------------------------------------------------------


class PlanExtra(BaseModel):
    model_config = _lenient

    param: str
    values: list[Any] | None = None
    range: dict[str, Any] | None = None
    reason: str = ""


class PlanIn(BaseModel):
    model_config = _lenient

    tuning: list[str] = Field(default_factory=list)
    extras: list[PlanExtra] = Field(default_factory=list)
    notes: str = ""


# -- trials -----------------------------------------------------------------------


class TrialIn(BaseModel):
    model_config = _lenient

    config: dict[str, Any]
    source: str = "self"  # a policy can only ever claim "self" here
    reported_metrics: dict[str, Any] = Field(default_factory=dict)
    reported_objective_value: float | None = None
    reported_feasible: bool | None = None
    failure_class: str = ""
    duration_seconds: float | None = None
    notes: str = ""


class TrialOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    seq: int
    config: dict[str, Any]
    config_hash: str
    source: str
    run_id: int | None
    reported_metrics: dict[str, Any]
    reported_objective_value: float | None
    reported_feasible: bool | None
    failure_class: str
    duration_seconds: float | None
    notes: str
    created_at: datetime | None = None


# -- delegated launches --------------------------------------------------------------


class LaunchIn(BaseModel):
    model_config = _lenient

    engine_args: dict[str, Any]
    gpu_indices: list[int] = Field(min_length=1)
    port: int | None = None


class LaunchOut(BaseModel):
    id: int
    status: str  # queued|launching|ready|serving|releasing|failed|released
    endpoint_url: str = ""
    launch_command: str = ""
    failure_class: str = ""
    error: str = ""
    # The canonical (merged, pruned) config — the echo the policy treats as
    # truth — and where it steps outside the declared space.
    config: dict[str, Any] = Field(default_factory=dict)
    deviations: list[dict[str, Any]] = Field(default_factory=list)


# -- benchmarks ------------------------------------------------------------------------


class ExternalBenchmarkIn(BaseModel):
    model_config = _lenient

    port: int
    engine_args: dict[str, Any]
    gpu_indices: list[int] = Field(min_length=1)
    suite: str = "screen"


class LaunchBenchmarkIn(BaseModel):
    model_config = _lenient

    suite: str = "screen"


class BenchmarkSummary(BaseModel):
    objective_value: float | None = None
    feasible: bool = False
    constraints: list[float | None] = Field(default_factory=list)
    breaches: list[str] = Field(default_factory=list)


class BenchmarkOut(BaseModel):
    id: int
    status: str  # queued|health_check|benching|succeeded|failed|killed
    suite: str = "screen"
    summary: BenchmarkSummary | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    failure_class: str = ""
    error: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    deviations: list[dict[str, Any]] = Field(default_factory=list)


# -- contenders ---------------------------------------------------------------------


class ContenderLaunchSpec(BaseModel):
    model_config = _lenient

    engine_args: dict[str, Any]
    image: str = Field(min_length=1)
    env: dict[str, str] = Field(default_factory=dict)
    volumes: dict[str, str] = Field(default_factory=dict)
    # engine/torch/CUDA versions + image digest, as the policy knows them —
    # what makes the verdict promotable.
    environment: dict[str, Any] = Field(default_factory=dict)


class ContenderIn(BaseModel):
    model_config = _lenient

    rank: int = Field(ge=1)
    launch_spec: ContenderLaunchSpec
    evidence: dict[str, Any] = Field(default_factory=dict)
    trial_ids: list[int] = Field(default_factory=list)


class ContendersPut(BaseModel):
    model_config = _lenient

    contenders: list[ContenderIn]


class ContenderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    rank: int
    launch_spec: dict[str, Any]
    config_hash: str
    status: str
    evidence: dict[str, Any]
    trial_ids: list[int]
    deviations: list[dict[str, Any]] | None = None
    run_id: int | None = None
    skip_reason: str = ""


class ContendersEcho(BaseModel):
    """The PUT response: what was accepted, and what was refused and why —
    rejected entries are echoed rather than 422ing the whole list, so one bad
    spec cannot cost a policy its good ones."""

    accepted: list[ContenderOut]
    rejected: list[dict[str, Any]] = Field(default_factory=list)


class ServingIn(BaseModel):
    model_config = _lenient

    port: int


# -- events ------------------------------------------------------------------------


class PolicyEventIn(BaseModel):
    model_config = _lenient

    level: str = "info"
    message: str = Field(min_length=1, max_length=4000)


# -- human-facing session views -------------------------------------------------------


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    campaign_id: int
    policy_id: int
    machine_id: int | None
    status: str
    gpu_indices: list[int]
    ports: list[int]
    container_name: str
    started_at: datetime | None
    first_heartbeat_at: datetime | None
    last_heartbeat_at: datetime | None
    policy_phase: str
    policy_message: str
    policy_progress: float | None
    policy_status: str = ""
    search_end_reason: str = ""
    capabilities: list[str]
    contract_version: str
    sdk_version: str
    plan: dict[str, Any]
    finalized_at: datetime | None
    serving_contender_id: int | None
    exit_code: int | None
    failure_class: str
    error: str
    created_at: datetime | None = None


class SessionDetailOut(SessionOut):
    search_deadline: datetime | None = None
    hard_deadline: datetime | None = None
    trials: list[TrialOut] = Field(default_factory=list)
    contenders: list[ContenderOut] = Field(default_factory=list)


# -- coverage (platform-derived, human-facing) -----------------------------------------


class AxisValueCoverage(BaseModel):
    """One declared value on one axis, and whether a trial has reached it."""

    value: Any
    covered: bool
    trials: int = 0


class AxisCoverage(BaseModel):
    param: str
    values: list[AxisValueCoverage] = Field(default_factory=list)
    covered: int = 0
    total: int = 0


class SpaceCoverage(BaseModel):
    """What part of the declared space this session has actually searched.

    Computed from the platform's own trial ledger against its own space
    expansion — authoritative, not the policy's word. For an enumerable space
    (grid/tied/range within the candidate cap) `cells_total`/`cells_covered`
    and `remaining` are exact; for a space too large to enumerate they are null
    and only the per-axis view and the policy's self-reported estimate apply.
    """

    enumerable: bool
    cells_total: int | None = None
    cells_covered: int = 0
    off_space: int = 0  # trials that landed outside the declared space (deviations)
    remaining: list[dict[str, Any]] = Field(default_factory=list)
    axes: list[AxisCoverage] = Field(default_factory=list)
    # Echo of the policy's own last coverage heartbeat, badged self-reported.
    policy_declared: PolicyCoverage | None = None
