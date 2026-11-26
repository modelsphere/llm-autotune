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
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.evaluation.benchmark_spec import BenchmarkSpec

CONTRACT_VERSION = "1.11"

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
    # How long one contender's validation benchmark takes, and how long its
    # model takes to come up first. Both None by default: the platform learns
    # them from this campaign's own runs (control/orchestrator/timing.py) and
    # reserves that much per contender at the end of the window. Set either to
    # pin it, for a model whose first night has nothing to learn from yet.
    approx_minutes_each: int | None = Field(default=None, ge=5, le=240)
    model_startup_minutes: int | None = Field(default=None, ge=0, le=120)
    # Overrides the derived default: the validation launches, side by side on
    # the GPU budget, in rounds of one contender's time, + 15.
    validation_reserve_minutes: int | None = Field(default=None, ge=5, le=720)
    # The night's GPU budget: how many cards the session holds at once, which
    # every launch it makes (search trials, repeats, validation) shares. A
    # launch takes as many as its config uses (tp × dp × pp), so the budget,
    # not a count of launches, is what limits parallelism. None = the whole
    # machine on a box the night has to itself; on a node pool, the cards of
    # the widest config in the space (one launch at a time).
    cards: int | None = Field(default=None, ge=1, le=64)
    # Measure production with the validation benchmark next to the contenders,
    # so the verdict says whether they beat it under the same conditions.
    validate_production: bool = True
    heartbeat_timeout_s: int | None = Field(default=None, ge=30, le=3600)
    # Productivity watchdog: a SEARCHING session with nothing running and no
    # delegated request for this long earns a strike; `search_idle_strikes`
    # consecutive strikes (any activity resets the count) declares the search
    # over and finalizes it — the enforceable backstop for a policy that
    # forgets to signal "exhausted" or livelocks while still heartbeating.
    search_idle_timeout_s: int | None = Field(default=None, ge=30, le=3600)
    search_idle_strikes: int = Field(default=3, ge=1, le=10)
    # Parameters the policy may not change (fnmatch globs over canonical
    # keys), enforced at every launch, benchmark and contender. None = the
    # platform default (space.DEFAULT_FORBIDDEN_PARAMS: flags that change
    # answers rather than speed); [] = nothing forbidden.
    forbidden_params: list[str] | None = Field(default=None, max_length=200)
    # Knobs for the policy itself (an agent's model name, effort, budget…),
    # handed over verbatim in the manifest as `policy.params`. The platform
    # never reads them.
    params: dict[str, Any] = Field(default_factory=dict)
    # `env.<NAME>` keys are checked against the vocabulary dumped from the
    # campaign's image: a variable the engine does not read would be ignored
    # silently. With no vocabulary for the image, "refuse" (the default)
    # refuses env keys outright; "warn" lets them through with a warning.
    env_without_vocabulary: Literal["refuse", "warn"] = "refuse"
    # (1.9) Other images of the campaign's engine a launch may name, besides
    # the campaign's own: same model, recipe and configuration, another image.
    images: list[str] = Field(default_factory=list, max_length=32)
    # (1.9) A launch may mount files into its engine container (LaunchIn.files):
    # a tuned kernel configuration, a chat template, a replaced module. Off
    # unless the campaign allows it. `file_paths` narrows where: a mounted
    # path must be under one of them; empty = any absolute path.
    files: bool = False
    file_paths: list[str] = Field(default_factory=list, max_length=32)
    # (1.11) Existing benchmarks a probe may run by slug (LaunchBenchmarkIn).
    benchmark_slugs: list[str] = Field(default_factory=list, max_length=8)

    def forbidden(self) -> list[str]:
        """The forbidden-parameter patterns in force for this campaign."""
        from app.control.search.space import DEFAULT_FORBIDDEN_PARAMS

        if self.forbidden_params is None:
            return list(DEFAULT_FORBIDDEN_PARAMS)
        return [p.strip() for p in self.forbidden_params if p.strip()]

    def validation_launches(self) -> int:
        """Validation runs at the end of the night: the contenders, and
        production next to them."""
        return self.max_contenders + (1 if self.validate_production else 0)

    def reserve_minutes(self, per_contender: int, rounds: int | None = None) -> int:
        """Validation time held back at the end of the window, given what one
        contender costs (policy_lifecycle.contender_minutes) and how many
        rounds the launches take side by side on the GPU budget (one launch
        per round when unknown)."""
        if self.validation_reserve_minutes is not None:
            return self.validation_reserve_minutes
        return (rounds or self.validation_launches()) * per_contender + 15


# -- registry (human-facing, JWT) -----------------------------------------------


class PolicyBase(BaseModel):
    name: str = Field(min_length=2, max_length=128)
    description: str = ""
    # The container to run. An external policy has none to run and may leave
    # this empty, or name what it is for people to read.
    image: str = Field(default="", max_length=255)
    repo_url: str = ""
    version: str = ""
    # Run outside the platform: no container is started, and the campaign's
    # owner asks for the session's token (POST /campaigns/{id}/session-token)
    # and drives the same policy API from wherever the policy runs.
    external: bool = False
    # Off by default: most policies delegate, asking the platform to launch
    # engines, and need neither. A self-serving policy, which runs engines in
    # its own container, turns both on. (Rows registered before keep theirs.)
    gpus_in_container: bool = False
    # Mount the campaign's weights at /model. False for a delegated-only policy:
    # it never reads them, and on k8s the mount would pin a placement-free
    # controller pod to a weights-bearing node.
    needs_model: bool = False
    # Plain env, shown to anyone who can read the registry and rendered into
    # the container's launch command. Secrets go in `secret_env`.
    env: dict[str, str] = Field(default_factory=dict)
    ports: int = Field(default=4, ge=1, le=16)
    # The LLM endpoint (Settings → LLM endpoints) an agent policy calls; at
    # launch it sets LLM_BASE_URL, LLM_MODEL and LLM_API_KEY. None: no model.
    llm_endpoint_id: int | None = None

    @model_validator(mode="after")
    def _a_container_or_none(self) -> Self:
        if not self.external:
            if not self.image.strip():
                raise ValueError("image: the container the platform runs for this policy")
            return self
        if self.gpus_in_container or self.needs_model:
            raise ValueError(
                "an external policy has no container on the machine: "
                "gpus_in_container and needs_model do not apply to it"
            )
        return self


class PolicyIn(PolicyBase):
    # Write-only env for credentials (an agent policy's LLM API key):
    # encrypted at rest, never returned, redacted from the launch command.
    # None keeps what is stored; a key with an empty value keeps its stored
    # value; a key left out is removed; {} clears them all.
    secret_env: dict[str, str] | None = None


class PolicyOut(PolicyBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    owner_id: int
    created_at: datetime | None = None
    # The names of the secret variables, never their values.
    secret_env_keys: list[str] = Field(default_factory=list)


class SessionTokenOut(BaseModel):
    """The credential of an external policy's session, shown once."""

    session_id: int
    # Sent as X-API-Key on every policy API call. Not stored: asking again
    # issues a new one and ends this one.
    token: str
    expires_at: datetime
    # Where the policy API is, as the platform is configured to be reached
    # from outside ("" when it is not).
    api_url: str = ""


# -- the manifest (policy-facing) -------------------------------------------------


class ManifestModel(BaseModel):
    engine: str
    image: str
    model_path: str
    served_model_name: str
    extra_env: dict[str, Any] = Field(default_factory=dict)
    extra_volumes: dict[str, Any] = Field(default_factory=dict)
    # 1.5: read from the model's own config.json (engines/model_facts.py):
    # layers, KV heads, experts and how many are active, built-in draft layers
    # (draft_layers), attention layout, dtype, context. Empty until read.
    facts: dict[str, Any] = Field(default_factory=dict)
    # 1.6: the configuration every launch starts from, as the engine chart
    # reads it ({engine, chart, values}; app/control/recipe), the space's base
    # applied. A config's keys are addresses into it: a flag, env.<NAME>,
    # prefill./decode.<flag> (a pd role's flags) or values.<path> (any chart
    # value). A winner is published as this document with its config applied.
    recipe: dict[str, Any] = Field(default_factory=dict)


class ManifestHardware(BaseModel):
    machine: str
    gpu_type: str = ""
    gpu_indices: list[int] = Field(default_factory=list)
    ports: list[int] = Field(default_factory=list)
    gpus_visible_in_container: bool = True
    # 1.2: a k8s node pool. `gpu_indices` are then slots (how many cards the
    # session may use at once), not devices: each launch's pod goes to any
    # node with room, and one launch's cards are always on one node, so no
    # launch may need more than `node_gpu_max`.
    pool: bool = False
    node_count: int = 0
    node_gpu_max: int = 0


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
    # (1.3) What the benchmark sends, module by module, from its locked params:
    # [{scenario, module, family, params, reads_as}] (app/measures/workload.py).
    # Empty when the benchmark platform could not be asked.
    workload: list[dict[str, Any]] = Field(default_factory=list)


class ManifestServices(BaseModel):
    launch: bool = True
    benchmarks: dict[str, BenchmarkSuite] = Field(default_factory=dict)
    state: bool = True
    # 1.3: engine logs, structured benchmark results, run environments, and
    # the configs/check dry run.
    diagnostics: bool = False
    # 1.4: PUT /session/digest and GET /history — what other campaigns learned.
    history: bool = False
    # 1.5: a config may set environment variables as `env.<NAME>` keys; the
    # platform puts them into the engine container's env. GET
    # /engine/vocabulary lists the flags and variables the image reads, with
    # each one's recommendation level.
    env_keys: bool = False
    vocabulary: bool = False
    # 1.6: config keys may also address a pd role's flags (`prefill.`/`decode.`)
    # and chart values (`values.<path>`); the manifest carries the recipe every
    # launch starts from, and /configs/check returns the recipe it builds.
    recipes: bool = False
    # 1.7: `production` is the platform's production config, built from the
    # Baselines record or, with none, from the campaign's recipe: what it runs
    # (`engine_args`, `env`), the config that launches it (`config`) and where
    # it came from (`source`).
    production_config: bool = False


class ManifestProduction(BaseModel):
    """Production: what serves today, as the platform launches it when it
    measures production next to the contenders."""

    # What it runs: every engine flag and env variable, read from the recipe
    # the platform builds (the recipe's own, with the recorded args applied).
    engine_args: dict[str, Any] = Field(default_factory=dict)
    env: dict[str, str] = Field(default_factory=dict)
    image: str = ""
    # (1.7) The config a policy sends to launch exactly production: the keys
    # over the campaign's anchor, so {} when production is the recipe as
    # written.
    config: dict[str, Any] = Field(default_factory=dict)
    # (1.7) "baselines": a recorded reference; "recipe": none recorded, so the
    # campaign's recipe as written; "campaign": the campaign's fields and base.
    source: str = ""
    launch_command: str = ""


class SessionManifest(BaseModel):
    contract: dict[str, str] = Field(default_factory=lambda: {"version": CONTRACT_VERSION})
    session_id: int
    campaign_id: int
    # name, image, version — and (1.3) `params`, the campaign's knobs for it.
    policy: dict[str, Any]
    model: ManifestModel
    hardware: ManifestHardware
    objective: dict[str, ManifestObjective]
    search_space: dict[str, Any]
    # `deviations` says how strictly the declared space binds; (1.3)
    # `forbidden` lists the parameter patterns the policy may not change.
    space_policy: dict[str, Any] = Field(
        default_factory=lambda: {"deviations": "allowed_if_declared", "forbidden": []}
    )
    production: ManifestProduction = Field(default_factory=ManifestProduction)
    time: ManifestTime
    contenders: ManifestContenders
    services: ManifestServices
    prior: dict[str, Any] = Field(default_factory=dict)
    # (1.3) The measures catalogue (docs/api/measures.md): the names, units,
    # directions and statistics a benchmark's `measures` are reported in.
    measures: list[dict[str, Any]] = Field(default_factory=list)


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
    # The cards the config runs on, as many as it uses (tp×dp×pp). (1.8)
    # Empty: the platform takes that many free cards of the allocation itself.
    gpu_indices: list[int] = Field(default_factory=list)
    port: int | None = None
    # (1.9) Run this image instead of the campaign's: one of the images the
    # campaign allows (`policy_settings.images`). Empty: the campaign's.
    image: str = ""
    # (1.9) Files to mount into the engine container, by absolute path, read
    # only, in place before the engine starts: {"/path": "<content>"}. Needs
    # `policy_settings.files`. Stored with the launch, so a relaunch of it
    # mounts the same files.
    files: dict[str, str] = Field(default_factory=dict)

    @field_validator("files")
    @classmethod
    def _files_fit(cls, files: dict[str, str]) -> dict[str, str]:
        check_launch_files(files)
        return files


# (1.9) What a launch may mount: they travel in one ConfigMap, whose limit is
# 1 MiB, so half of that, in at most 16 files.
MAX_LAUNCH_FILES = 16
MAX_LAUNCH_FILE_BYTES = 512 * 1024


def check_launch_files(files: dict[str, str]) -> None:
    if len(files) > MAX_LAUNCH_FILES:
        raise ValueError(f"at most {MAX_LAUNCH_FILES} files")
    total = 0
    for path, content in files.items():
        parts = path.split("/")
        if not path.startswith("/") or path.endswith("/") or ".." in parts or "" in parts[1:]:
            raise ValueError(f"{path!r}: a file's path is absolute, normalized, and names a file")
        total += len(content.encode("utf-8"))
    if total > MAX_LAUNCH_FILE_BYTES:
        raise ValueError(f"the files come to {total} bytes; at most {MAX_LAUNCH_FILE_BYTES}")


class LaunchFile(BaseModel):
    path: str
    sha256: str
    bytes: int


def launch_files(files: dict[str, str] | None) -> list[LaunchFile]:
    import hashlib

    return [
        LaunchFile(
            path=path,
            sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            bytes=len(content.encode("utf-8")),
        )
        for path, content in sorted((files or {}).items())
    ]


class ExecIn(BaseModel):
    """(1.9) A shell line to run in a launch's engine container."""

    model_config = _lenient

    command: str = Field(min_length=1, max_length=8000)
    timeout_s: int = Field(default=60, ge=1, le=600)


class ExecOut(BaseModel):
    exit_code: int
    # stdout then stderr, the last 64 KB.
    output: str
    tainted: bool = True


class LaunchOut(BaseModel):
    id: int
    status: str  # queued|launching|ready|serving|releasing|failed|released
    # (1.8) The cards it holds (slots, on a node pool).
    gpu_indices: list[int] = Field(default_factory=list)
    # (1.9) The image the launch named; "" = the campaign's.
    image: str = ""
    # (1.9) A command was run in its engine (POST /launches/{id}/exec).
    tainted: bool = False
    # (1.9) The files mounted into its engine, by path and digest.
    files: list[LaunchFile] = Field(default_factory=list)
    endpoint_url: str = ""
    launch_command: str = ""
    failure_class: str = ""
    error: str = ""
    # The canonical (merged, pruned) config — the echo the policy treats as
    # truth — and where it steps outside the declared space.
    config: dict[str, Any] = Field(default_factory=dict)
    deviations: list[dict[str, Any]] = Field(default_factory=list)
    # 1.2: while the cluster has no room for the launch, `status` is "queued"
    # and these say why and since when. It starts by itself once cards free up.
    waiting_reason: str = ""
    waiting_since: datetime | None = None
    # (1.10) What it runs, as one document: the campaign's recipe with the
    # config and the launch's image applied ({engine, chart, values,
    # image_digest}), and the identity of that recipe with the launch's files.
    # Without files, `config_hash` is the recipe's own hash.
    recipe: dict[str, Any] = Field(default_factory=dict)
    config_hash: str = ""


# -- benchmarks ------------------------------------------------------------------------


class ExternalBenchmarkIn(BaseModel):
    model_config = _lenient

    port: int
    engine_args: dict[str, Any]
    # Empty only on a machine with no cards (see LaunchIn).
    gpu_indices: list[int] = Field(default_factory=list)
    suite: str = "screen"
    # (1.8) See LaunchBenchmarkIn.
    benchmark: BenchmarkSpec | None = None


class LaunchBenchmarkIn(BaseModel):
    model_config = _lenient

    suite: str = "screen"
    # (1.8) A probe: measure with this workload instead of a suite's. The
    # platform creates the benchmark (the same description is always the same
    # benchmark) and answers the result, without judging it by the campaign's
    # objective or ranking it with the campaign's results. `suite` is ignored.
    benchmark: BenchmarkSpec | None = None
    # (1.11) A probe on an existing benchmark, by slug: one the campaign lists
    # in `policy_settings.benchmark_slugs`. Answered like any probe.
    benchmark_slug: str = ""


class BenchmarkSummary(BaseModel):
    objective_value: float | None = None
    feasible: bool = False
    constraints: list[float | None] = Field(default_factory=list)
    breaches: list[str] = Field(default_factory=list)


class ScenarioMeasures(BaseModel):
    """One module's results as measures (docs/api/measures.md)."""

    scenario: str  # the module's instance key: `replay`, `perf_guidellm_sweep#2`
    module: str
    kind: str  # sweep | single | replay | quality
    # Which level the operating point is (`c8`; null for anything but a
    # sweep), and how it was chosen.
    operating_level: str | None = None
    operating_rule: str = ""
    # Measure key (`ttft_ms@p99`, `ttft_ms@p90[32k_64k]`) → value, at the
    # operating point. Absent keys were not reported — never 0.
    operating: dict[str, float] = Field(default_factory=dict)
    # A sweep's other points: `peak` and each `c<N>`.
    points: dict[str, dict[str, float]] = Field(default_factory=dict)
    # Every measure key this module can report at all.
    supports: list[str] = Field(default_factory=list)


class ModuleProgress(BaseModel):
    key: str
    module: str = ""
    status: str = ""


class BenchmarkOut(BaseModel):
    id: int
    status: str  # queued|health_check|benching|succeeded|failed|killed
    suite: str = "screen"  # screen | verify | probe (1.8)
    # (1.8) The benchmark it ran on, for a probe; "" for a suite's.
    benchmark_slug: str = ""
    # (1.8) Each module run of the benchmark, in order, with its state
    # (pending, running, done, failed, skipped) and the key its metrics are
    # reported under. While the benchmark runs, `metrics` already holds the
    # modules that are done; `summary` and `measures` wait for the end.
    modules: list[ModuleProgress] = Field(default_factory=list)
    summary: BenchmarkSummary | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    # (1.3) The same results as measures, one entry per module. `metrics` keeps
    # the raw LLMBench keys.
    measures: list[ScenarioMeasures] = Field(default_factory=list)
    failure_class: str = ""
    error: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    deviations: list[dict[str, Any]] = Field(default_factory=list)


# -- configs/check (1.3) ------------------------------------------------------------


class ConfigCheckIn(BaseModel):
    model_config = _lenient

    engine_args: dict[str, Any]
    # (1.9) Check against another of the campaign's images.
    image: str = ""


class ConfigProblem(BaseModel):
    # placement | forbidden | not_renderable | round_trip | bad_key | safety
    kind: str
    param: str = ""
    message: str


class ConfigCheckOut(BaseModel):
    """What a launch of this config would meet, without spending one.

    `ok` is false when a launch, benchmark or contender with this config would
    be refused (`problems`). `warnings` never refuse anything: flags the
    platform's catalog does not know (the engine may still accept them) and
    how the config steps outside the declared space (`deviations`).
    """

    ok: bool
    config: dict[str, Any] = Field(default_factory=dict)
    cards: int = 0
    command: list[str] = Field(default_factory=list)
    # The config's `env.<NAME>` keys as the container receives them (1.4),
    # set over the campaign's own extra_env.
    env: dict[str, str] = Field(default_factory=dict)
    problems: list[ConfigProblem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    deviations: list[dict[str, Any]] = Field(default_factory=list)
    # 1.6: the recipe a launch of this config runs, the pods it makes and the
    # cards each claims, and each role's engine command as the chart builds it
    # (the chart's own flags first, then the recipe's).
    recipe: dict[str, Any] = Field(default_factory=dict)
    shape: dict[str, Any] = Field(default_factory=dict)
    commands: dict[str, list[str]] = Field(default_factory=dict)


class VocabularyOut(BaseModel):
    """The flags and environment variables the campaign's engine image reads
    (1.5), best recommendation first.

    `status` is "ok", "partial" (some of it could not be read; `notes` says
    what), or "none" (nothing has been dumped from this image yet). Each entry:
    {kind: flag|env, name, type, default, choices, help, help_source, where,
    level: high|mid|low|unknown, when}. `level` is the platform's hand-written
    recommendation, `when` the situation it helps in; an entry labeled only for
    other hardware is left out.
    """

    status: str
    engine: str
    engine_version: str = ""
    image: str = ""
    total: int = 0
    notes: list[str] = Field(default_factory=list)
    entries: list[dict[str, Any]] = Field(default_factory=list)
    # The model's facts and its whole config.json, once read (1.5); empty
    # before. Here as well as in the manifest, which a policy reads once.
    model_facts: dict[str, Any] = Field(default_factory=dict)
    model_config_json: dict[str, Any] = Field(default_factory=dict)


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
    # A human asked it to wind down (Force stop) or to stop now (Abort); the
    # worker acts on its next tick.
    finalize_requested_at: datetime | None = None
    abort_requested_at: datetime | None = None
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
    # While STARTING: what the container is waiting on, read live from the
    # substrate (e.g. a pod no node can take yet). Empty otherwise.
    waiting: str = ""


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


# -- history (1.4) -------------------------------------------------------------


class DigestLesson(BaseModel):
    model_config = _lenient

    text: str = Field(max_length=2000)
    # How far it reaches: campaign (this workload and objective), model,
    # engine or hardware. Only the policy knows; the platform files it.
    scope: str = Field("campaign", max_length=32)


class DigestIn(BaseModel):
    """What a night learned, in the policy's words: written once its search is
    over, for other campaigns to read through GET /history. The conditions it
    was learned under are the platform's to state, not the policy's."""

    model_config = _lenient

    summary: str = Field(max_length=20000)
    lessons: list[DigestLesson] = Field(default_factory=list, max_length=50)
    # What failed and must not be repeated, with the cause.
    constraints: list[str] = Field(default_factory=list, max_length=50)
    open_questions: list[str] = Field(default_factory=list, max_length=20)


class HistoryConditions(BaseModel):
    """Under what a night's findings hold, from the platform's own records."""

    engine: str
    image: str
    model_path: str
    served_model_name: str
    gpu_type: str = ""
    cards: int | None = None
    benchmark_slug: str = ""
    objective: ManifestObjective
    forbidden: list[str] = Field(default_factory=list)


class HistoryVerdict(BaseModel):
    """One config the platform validated at the end of the night."""

    config: dict[str, Any]
    is_production: bool = False
    value: float | None = None
    # Value as a multiple of production's on the same night (>1 = higher).
    vs_production: float | None = None
    holds_redlines: bool = True
    # A finalist whose config IS production's: its vs_production measures the
    # validation's own noise, not an effect.
    same_as_production: bool = False


class HistoryEntry(BaseModel):
    campaign_id: int
    campaign_name: str
    session_id: int
    ended_at: datetime | None = None
    end_reason: str = ""
    conditions: HistoryConditions
    verdicts: list[HistoryVerdict] = Field(default_factory=list)
    digest: DigestIn | None = None
