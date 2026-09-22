"""Wire shapes for the agent API — what an LLM reads to write a report.

Five parts (launch, environment, benchmark, results, log) and three documents
(run, campaign, comparison) that embed the parts unchanged. A part fetched on
its own is the same object as the part inside a document, so an agent that
reads only documents and one that drills into parts see identical data.
See docs/api/agent-api.md for the contract these shapes implement.

Open-ended payloads (metrics, params, snapshots) stay `dict[str, Any]`: the
benchmark platform is external and its modules change, and a new metric must
land as data rather than as a schema change here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1

# -- parts -----------------------------------------------------------------


class LaunchOrigin(BaseModel):
    source: Literal["campaign"] = "campaign"
    campaign_id: int | None = None
    campaign_name: str = ""
    candidate_id: int | None = None


class LaunchRendered(BaseModel):
    # Exactly what the platform ran, as stored on the run. Empty when the run
    # never launched.
    docker_command: str = ""
    # The in-container server command, from the same engine adapter that
    # produced the docker line.
    engine_command: str = ""
    # What a person runs to serve this config outside the platform:
    # `sglang serve` / `vllm serve` on the real weights path, one flag per
    # line. The one to put in a report.
    serve_command: str = ""
    # canonical engine_args key -> the engine's own flag spelling.
    engine_flags: dict[str, str] = Field(default_factory=dict)


class LaunchConfigOut(BaseModel):
    engine: str
    image: str = ""
    model_path: str = ""
    served_model_name: str = ""
    service_port: int = 0
    engine_args: dict[str, Any] = Field(default_factory=dict)
    extra_env: dict[str, str] = Field(default_factory=dict)
    extra_volumes: dict[str, str] = Field(default_factory=dict)
    gpu_type: str = ""


class LaunchPart(BaseModel):
    kind: Literal["launch"] = "launch"
    schema_version: int = SCHEMA_VERSION
    run_id: int
    config: LaunchConfigOut
    cards: int
    rendered: LaunchRendered
    origin: LaunchOrigin


class MachineOut(BaseModel):
    name: str = ""
    gpu_type: str = ""
    gpu_count: int = 0
    driver: str = ""


class EnvironmentPart(BaseModel):
    kind: Literal["environment"] = "environment"
    schema_version: int = SCHEMA_VERSION
    run_id: int
    machine: MachineOut
    gpu_indices: list[int] = Field(default_factory=list)
    # Lifted from the snapshot when the probe recorded them. Absent, not
    # empty, when it did not: an agent must not invent a version.
    card_type: str | None = None
    engine_version: str | None = None
    torch_version: str | None = None
    cuda_version: str | None = None
    gpu_name: str | None = None
    image_digest: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    # The whole recorded snapshot, for anything not lifted above.
    snapshot: dict[str, Any] = Field(default_factory=dict)


class BenchmarkModuleOut(BaseModel):
    key: str
    module_name: str
    order_index: int = 0
    weight: float | None = None
    is_scenario: bool = False
    label: str = ""
    # Frozen: what this run was measured against.
    params: dict[str, Any] = Field(default_factory=dict)
    # LLMBench's own metric roles and thresholds for this module, frozen.
    metric_configs: list[dict[str, Any]] = Field(default_factory=list)


class BenchmarkLLMBenchOut(BaseModel):
    slug: str = ""
    benchmark_id: int | None = None
    url: str = ""
    submission_id: str = ""
    submission_url: str = ""
    config_hash_frozen: str = ""
    dataset_build_id: str = ""


class BenchmarkPlatformOut(BaseModel):
    """What the platform keeps about the benchmark on top of the benchmark
    platform's own document: which metric decides better, and the service
    level the campaign's objective holds every candidate to."""

    ranking_metric: str = ""
    slo: dict[str, Any] = Field(default_factory=dict)
    gate_text: str = ""
    max_run_minutes: int | None = None
    objective: dict[str, Any] = Field(default_factory=dict)


class BenchmarkPart(BaseModel):
    kind: Literal["benchmark"] = "benchmark"
    schema_version: int = SCHEMA_VERSION
    run_id: int | None = None
    llmbench: BenchmarkLLMBenchOut
    modules: list[BenchmarkModuleOut] = Field(default_factory=list)
    platform: BenchmarkPlatformOut


class ResultsHeadline(BaseModel):
    ranking_metric: str = ""
    ranking_value: float | None = None
    total_tpm_card_norm: float | None = None
    output_tpm_card_norm: float | None = None
    peak_concurrency: float | None = None
    reported_concurrency: float | None = None
    meets_gate: bool | None = None
    quality: dict[str, float] = Field(default_factory=dict)


class ScenarioLevel(BaseModel):
    concurrency: int
    metrics: dict[str, Any] = Field(default_factory=dict)


class ScenarioOut(BaseModel):
    key: str
    # "sweep": synthetic prompts, a concurrency search, per-level `levels`.
    # "replay": recorded traffic at one concurrency — no token counts, no
    # levels; everything it measured is in `metrics`.
    kind: Literal["sweep", "replay"] = "sweep"
    label: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    # Module-level (not per-concurrency) metrics of this scenario.
    metrics: dict[str, Any] = Field(default_factory=dict)
    levels: list[ScenarioLevel] = Field(default_factory=list)
    # The highest-throughput level that met the SLO (a replay: the level it ran
    # at): concurrency, total/input/output tok/s per GPU, TTFT at the gate's
    # percentile, per-request output tok/s. {} when no level passed.
    best_level: dict[str, Any] = Field(default_factory=dict)


class QualityOut(BaseModel):
    key: str
    module_name: str = ""
    scores: dict[str, float] = Field(default_factory=dict)
    passed: bool | None = None
    error: str = ""


class VerdictOut(BaseModel):
    module: str
    metric: str
    role: str  # redline | display | quality_floor
    min: float | None = None
    max: float | None = None
    actual: float | None = None
    ok: bool | None = None


class ModuleStatusOut(BaseModel):
    key: str
    module_name: str = ""
    status: str = ""
    passed: bool | None = None
    score: float | None = None
    error: str = ""


class FailureOut(BaseModel):
    failure_class: str = ""
    error: str = ""
    run_status: str = ""
    log: str = ""


class ResultsPart(BaseModel):
    kind: Literal["results"] = "results"
    schema_version: int = SCHEMA_VERSION
    run_id: int
    status: Literal["measured", "failed", "running", "queued"]
    passed: bool | None = None
    feasible: bool | None = None
    score: float | None = None
    objective_value: float | None = None
    headline: ResultsHeadline = Field(default_factory=ResultsHeadline)
    scenarios: list[ScenarioOut] = Field(default_factory=list)
    quality: list[QualityOut] = Field(default_factory=list)
    verdicts: list[VerdictOut] = Field(default_factory=list)
    modules: list[ModuleStatusOut] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    failure: FailureOut | None = None
    links: dict[str, str] = Field(default_factory=dict)


# -- documents -------------------------------------------------------------


class RunDocument(BaseModel):
    kind: Literal["run"] = "run"
    schema_version: int = SCHEMA_VERSION
    run_id: int
    launch: LaunchPart
    environment: EnvironmentPart
    benchmark: BenchmarkPart
    results: ResultsPart
    links: dict[str, str] = Field(default_factory=dict)


class CampaignRunRow(BaseModel):
    run_id: int
    name: str = ""
    status: str
    kind: str = ""
    engine: str = ""
    engine_version: str = ""
    image: str = ""
    cards: int = 0
    engine_args: dict[str, Any] = Field(default_factory=dict)
    machine: str = ""
    headline: ResultsHeadline = Field(default_factory=ResultsHeadline)
    passed: bool | None = None
    # The config the campaign measured as its reference, when it ran one.
    is_baseline: bool = False
    finished_at: datetime | None = None
    links: dict[str, str] = Field(default_factory=dict)


class CampaignSummary(BaseModel):
    id: int
    name: str
    status: str
    model_name: str = ""
    engine: str = ""
    benchmark_slug: str = ""
    ranking_metric: str = ""
    run_count: int = 0
    links: dict[str, str] = Field(default_factory=dict)


class CampaignDocument(BaseModel):
    kind: Literal["campaign"] = "campaign"
    schema_version: int = SCHEMA_VERSION
    id: int
    name: str
    status: str
    model_name: str = ""
    model_path: str = ""
    engine: str = ""
    benchmark: BenchmarkPart
    baseline_run_id: int | None = None
    runs: list[CampaignRunRow] = Field(default_factory=list)
    links: dict[str, str] = Field(default_factory=dict)


class Delta(BaseModel):
    baseline: float | None = None
    attempt: float | None = None
    pct: float | None = None
    better: Literal["higher", "lower"] = "higher"
    improved: bool | None = None


class ScenarioDeltas(BaseModel):
    key: str
    label: str = ""
    summary: dict[str, Delta] = Field(default_factory=dict)
    levels: list[dict[str, Any]] = Field(default_factory=list)  # {concurrency, metrics: {k: Delta}}
    best_level: dict[str, Delta] = Field(default_factory=dict)
    # Replay scenarios only: deltas over the module-level metrics.
    metrics: dict[str, Delta] = Field(default_factory=dict)


class Deltas(BaseModel):
    headline: dict[str, Delta] = Field(default_factory=dict)
    scenarios: list[ScenarioDeltas] = Field(default_factory=list)
    quality: dict[str, Delta] = Field(default_factory=dict)


class ConfigDiff(BaseModel):
    engine: dict[str, Any] | None = None
    image: dict[str, Any] | None = None
    model_path: dict[str, Any] | None = None
    engine_args: dict[str, Any] = Field(default_factory=dict)  # {added, removed, changed}
    extra_env: dict[str, Any] = Field(default_factory=dict)
    cards: dict[str, Any] | None = None
    changed: bool = False


class VerdictChange(BaseModel):
    module: str
    metric: str
    role: str = "redline"
    baseline_ok: bool | None = None
    attempt_ok: bool | None = None


class ComparisonRun(BaseModel):
    run_id: int
    label: str
    launch: LaunchPart
    environment: EnvironmentPart
    results: ResultsPart


class ComparisonAttempt(ComparisonRun):
    position: int
    diff_vs_baseline: ConfigDiff
    diff_vs_previous: ConfigDiff
    deltas: Deltas
    deltas_vs_previous: Deltas
    verdict_changes: list[VerdictChange] = Field(default_factory=list)


class NotComparableReason(BaseModel):
    code: str
    run_id: int | None = None
    module: str = ""
    field: str = ""
    baseline: Any = None
    attempt: Any = None
    detail: str = ""


class SeriesLine(BaseModel):
    run_id: int
    label: str
    points: list[list[float]] = Field(default_factory=list)


class Series(BaseModel):
    scenario: str
    scenario_label: str = ""
    metric: str
    x: str = "concurrency"
    unit: str = ""
    better: Literal["higher", "lower"] = "higher"
    lines: list[SeriesLine] = Field(default_factory=list)


class CatalogEntry(BaseModel):
    key: str
    label: str
    unit: str = ""
    better: Literal["higher", "lower"] = "higher"
    help: str = ""


class BestOut(BaseModel):
    overall: dict[str, Any] | None = None
    per_scenario: list[dict[str, Any]] = Field(default_factory=list)


class ComparisonDocument(BaseModel):
    kind: Literal["comparison"] = "comparison"
    schema_version: int = SCHEMA_VERSION
    comparable: bool
    reasons: list[NotComparableReason] = Field(default_factory=list)
    benchmark: BenchmarkPart
    baseline: ComparisonRun
    attempts: list[ComparisonAttempt] = Field(default_factory=list)
    best: BestOut = Field(default_factory=BestOut)
    series: list[Series] = Field(default_factory=list)
    catalog: dict[str, CatalogEntry] = Field(default_factory=dict)
    rendered: dict[str, Any] | None = None
    links: dict[str, str] = Field(default_factory=dict)


# -- reports ---------------------------------------------------------------


class ReportAssetIn(BaseModel):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    content_type: str = Field(default="image/png", max_length=64)
    data_base64: str


class ReportCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    baseline_run_id: int
    attempt_run_ids: list[int] = Field(min_length=1)
    comparable: bool = True
    markdown: str = Field(min_length=1)
    assets: list[ReportAssetIn] = Field(default_factory=list)
    generator: dict[str, Any] = Field(default_factory=dict)
    lang: Literal["en", "zh"] = "en"
    # The report this one translates (same runs); None for the first language.
    translation_of: int | None = None
    # Public names of the configs, baseline first. [] = Baseline / Optimized.
    labels: list[str] = Field(default_factory=list)


class ReportBlock(BaseModel):
    """One fenced block a report may carry, ready to paste."""

    block: Literal["chart", "table", "command"]
    type: str
    params: dict[str, str] = Field(default_factory=dict)
    description: str = ""
    markdown: str


class ReportTranslation(BaseModel):
    id: int
    lang: str
    title: str


class ReportOut(BaseModel):
    id: int
    title: str
    baseline_run_id: int
    attempt_run_ids: list[int]
    campaign_id: int | None = None
    comparable: bool
    generator: dict[str, Any] = Field(default_factory=dict)
    created_by_name: str = ""
    created_at: datetime | None = None
    asset_names: list[str] = Field(default_factory=list)
    url: str = ""
    lang: str = "en"
    translation_of: int | None = None
    labels: list[str] = Field(default_factory=list)


class ReportDetailOut(ReportOut):
    markdown: str
    comparison: dict[str, Any] = Field(default_factory=dict)
    # Every language this report exists in, itself included.
    translations: list[ReportTranslation] = Field(default_factory=list)
