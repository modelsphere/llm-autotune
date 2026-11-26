"""A benchmark described by what it measures, built on LLMBench by AutoTune.

A first-time user cannot be expected to know an LLMBench benchmark slug, or
what a dataset profile is. They can say what load they want: random prompts of
some length at some concurrencies, or a replay of recorded traffic. A spec is
that sentence; AutoTune turns it into an LLMBench benchmark, creates it under
its own group tag and locks it.

The slug is a hash of the spec, so the same workload is the same benchmark in
every campaign — results stay comparable across campaigns, and asking twice
does not clutter LLMBench with copies.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.config import get_settings
from app.evaluation.benchmarks import GROUP_TAG, BenchmarkRefused, Ensured, ensure_document
from app.evaluation.bfcl_sweep import BFCL_SCAN_MAX_SECONDS
from app.evaluation.llmbench import LLMBenchClient, instance_keys

SWEEP_MODULE = "perf_guidellm_sweep"

# What a sweep shows besides its score; the ranking itself is AutoTune's
# objective, applied to the raw metrics.
_SWEEP_METRICS = [
    {"key": "output_tps", "role": "score", "weight": 1.0, "formula": "ratio_capped",
     "baseline": 5000.0},
    {"key": "uptime", "role": "redline", "min_val": 0.95},
    {"key": "ttft_p50_ms", "role": "display"},
    {"key": "ttft_p99_ms", "role": "display"},
    {"key": "itl_p99_ms", "role": "display"},
    {"key": "request_output_tps", "role": "display"},
    {"key": "reported_concurrency", "role": "display"},
]
# Used only when LLMBench cannot tell us the replay module's own defaults.
_REPLAY_FALLBACK_METRICS = [
    {"key": "output_tpm", "role": "score", "weight": 1.0, "formula": "passthrough_scaled"},
    {"key": "uptime", "role": "redline", "min_val": 0.95},
    {"key": "ttft_p99_ms", "role": "display"},
    {"key": "total_time_p99_ms", "role": "display"},
]


QUALITY_MODULE = "opencompass"
# The quality suites the opencompass module runs, and the suffix of each one's
# parameters there (sample_cap_<s>, avg_k_<s>, pass_k_<s>). hle needs a judge
# model, so it is not offered.
QUALITY_TASKS: dict[str, str] = {
    "gpqa_diamond": "gpqa",
    "mmlu_pro": "mmlu",
    "aime2025": "aime",
    "ifeval": "ifeval",
    "livecodebench_v6": "lcb",
    "simpleqa": "simpleqa",
    "longbench_v2": "longbench",
}
DEFAULT_QUALITY_TASKS = ["gpqa_diamond"]


class QualitySpec(BaseModel):
    """A quality check run after the workload, on the same endpoint: the
    first `questions` of each task (a fixed subset, the same every run, so
    two configs answer the same questions), answered once each at
    temperature 0, `workers` at a time, in up to `max_tokens`. It judges
    nothing by itself; a campaign compares its scores with production's
    (app/quality.py)."""

    tasks: list[str] = Field(default_factory=lambda: list(DEFAULT_QUALITY_TASKS))
    questions: int = Field(default=100, ge=0, le=100_000)  # per task; 0 = all
    # A reasoning model may think for many thousands of tokens on a hard
    # question; an answer cut off there counts as wrong.
    max_tokens: int = Field(default=16384, ge=256, le=131_072)
    workers: int = Field(default=16, ge=1, le=256)
    max_seconds: int = Field(default=7200, ge=60, le=43_200)

    @field_validator("tasks")
    @classmethod
    def _known(cls, v: list[str]) -> list[str]:
        tasks = list(dict.fromkeys(t.strip() for t in v if t.strip()))
        unknown = [t for t in tasks if t not in QUALITY_TASKS]
        if unknown:
            raise ValueError(f"unknown quality tasks {unknown}; known: {sorted(QUALITY_TASKS)}")
        if not tasks:
            raise ValueError("at least one quality task")
        return tasks


BFCL_MODULE = "bfcl_v4"
BFCL_TASK_SETS = (
    "bfcl-v4-smoke-v1", "bfcl-v4-tool-use-v1",
    "bfcl-v4-multi-turn-long-context-v1",
)


class BFCLSpec(BaseModel):
    """A frozen official BFCL subset, using native streamed tool calls.

    Concurrency counts active cases, each of which can make several sequential
    HTTP requests. Accuracy is a share, not a percentage.
    """

    model_config = {"extra": "forbid"}
    task_set: Literal[
        "bfcl-v4-smoke-v1", "bfcl-v4-tool-use-v1",
        "bfcl-v4-multi-turn-long-context-v1",
    ] = "bfcl-v4-tool-use-v1"
    cases_per_category: int = Field(20, ge=1, le=200)
    concurrency: int = Field(2, ge=1, le=128)
    concurrency_grid: list[int] | None = Field(default=None, min_length=2, max_length=16)
    max_total_seconds: float | None = Field(default=None, ge=1, le=BFCL_SCAN_MAX_SECONDS)
    request_timeout_seconds: float = Field(120, ge=1, le=3600)
    max_seconds: float = Field(1800, ge=1, le=86400)
    request_retries: int = Field(0, ge=0, le=3)
    export_replay_trace: bool = False
    probe_endpoint: bool = False
    required_metrics: list[str] = Field(
        default_factory=lambda: ["accuracy", "passed_cases_per_hour", "ttft_p90_ms"])
    min_ttft_coverage: float = Field(1, ge=0, le=1)
    min_usage_coverage: float = Field(0, ge=0, le=1)
    min_cache_usage_coverage: float = Field(0, ge=0, le=1)

    @field_validator("concurrency_grid", mode="before")
    @classmethod
    def _strict_grid(cls, value):
        if value is not None and (not isinstance(value, list)
                                  or any(type(c) is not int for c in value)):
            raise ValueError("BFCL scan requires integer concurrency points")
        return value

    @model_validator(mode="after")
    def _smoke_count(self) -> BFCLSpec:
        if self.concurrency_grid is not None:
            grid = self.concurrency_grid
            if (grid != sorted(set(grid))
                    or any(type(c) is not int or not 1 <= c <= 128 for c in grid)):
                raise ValueError("concurrency_grid requires increasing unique integers in [1, 128]")
            budget = self.max_total_seconds or self.max_seconds * len(grid)
            if budget > BFCL_SCAN_MAX_SECONDS:
                raise ValueError(
                    "BFCL scan budget exceeds 47 hours; reduce max_seconds or set max_total_seconds"
                )
        if self.task_set == "bfcl-v4-smoke-v1" and self.cases_per_category != 1:
            raise ValueError("the BFCL smoke subset requires cases_per_category=1")
        return self


def bfcl_module(spec: BFCLSpec, *, quality: bool = False) -> dict:
    metrics = ["accuracy", "accuracy_simple_python", "accuracy_multiple", "accuracy_parallel",
               "accuracy_irrelevance", "accuracy_multi_turn_base",
               "accuracy_multi_turn_long_context", "passed_cases_per_hour", "case_count",
               "ttft_p50_ms", "ttft_p90_ms", "ttft_p99_ms", "request_e2e_p90_ms",
               "input_tpm", "output_tpm", "total_tpm", "cache_hit_rate", "ttft_coverage",
               "input_usage_coverage", "output_usage_coverage", "cache_usage_coverage"]
    return {
        "module_name": BFCL_MODULE,
        "params": spec.model_dump(exclude_none=True),
        "metric_configs": [
            {"key": key, "role": "score" if key == "accuracy" and not quality else "display",
             "formula": "passthrough"}
            for key in metrics
        ],
    }


class SweepSLO(BaseModel):
    """The latency promise a sweep level must keep (LLMBench's slo_*): in
    auto mode what the search looks for; in grid mode which level is
    reported (report_level slo_best). Unset: LLMBench's defaults."""

    model_config = ConfigDict(extra="forbid")

    ttft_ms: float = Field(default=120_000, gt=0)
    ttft_percentile: Literal["mean", "p50", "p90", "p99"] = "p99"
    min_request_output_tps: float = Field(default=10, ge=0)


class Workload(BaseModel):
    """One load put on the engine. Three kinds:

    - `sweep`: random prompts of `input_tokens`, answers of `output_tokens`,
      at each of `concurrencies`. Level c sends c × `requests_per_concurrency`
      requests, so every level is measured on the same sample per slot however
      fast the engine is; `max_seconds_per_level` caps a slow one. Fast, and
      blind to prefix-cache reuse. `mode: auto` searches for the highest
      concurrency that keeps the `slo` instead (doubling, then bisecting with
      short probes, then a confirmation run at full length).
    - `replay`: requests recorded from real traffic, replayed `concurrency` at
      a time — or, with `schedule: timed`, each on its recorded timing (a
      timed dataset; latency at production's own load). `dataset_profile`
      names an LLMBench collection profile (a rolling sample), or
      `dataset_frozen` a frozen dataset (a file that never changes); neither
      replays LLMBench's built-in example set (proves the pipeline, measures
      nothing). `requests` caps how many (0 = the whole dataset).
    - `bfcl`: a frozen official BFCL subset with native streamed tool calls.
    """

    kind: Literal["sweep", "replay", "bfcl"] = "sweep"
    input_tokens: int = Field(default=2048, ge=1, le=1_000_000)
    output_tokens: int = Field(default=512, ge=1, le=100_000)
    concurrencies: list[int] = Field(default_factory=lambda: [1, 4, 16, 64])
    requests_per_concurrency: int = Field(default=20, ge=1, le=10_000)
    max_seconds_per_level: int = Field(default=600, ge=10, le=7200)
    # A sweep's mode: `grid` runs `concurrencies`; `auto` searches.
    mode: Literal["grid", "auto"] = "grid"
    slo: SweepSLO | None = None
    # grid: which level is reported: the best that meets the SLO, the highest
    # throughput, or `fixed_level`.
    report_level: Literal["slo_best", "peak", "fixed"] = "slo_best"
    fixed_level: int | None = Field(default=None, ge=1)
    # auto: the search's bound, its probes, its stopping width (0 = automatic)
    # and its time budget.
    max_concurrency: int = Field(default=1024, ge=1, le=100_000)
    probe_requests_per_concurrency: int = Field(default=5, ge=1, le=100_000)
    probe_seconds: float = Field(default=900, gt=0, le=86_400)
    granularity: int = Field(default=0, ge=0)
    max_total_seconds: float = Field(default=7200, gt=0, le=86_400 * 2)
    dataset_profile: str = ""
    dataset_frozen: str = ""
    requests: int = Field(default=500, ge=0, le=1_000_000)
    concurrency: int = Field(default=16, ge=1, le=4096)
    # How a replay sends: `closed` keeps `concurrency` requests in flight;
    # `timed` sends every request of a timed dataset on its recorded timing (1x
    # production load), with no concurrency of its own and the whole dataset,
    # so `requests` and `concurrency` do not apply. Latency is what it measures.
    schedule: Literal["closed", "timed"] = "closed"
    # A replay (closed or timed) with every response forced to its recorded
    # length (LLMBench's fixed_output_lengths): each engine does the same work,
    # so the score measures speed rather than how long the sampled answers
    # were. In a timed replay this also keeps each session's next turn on the
    # same footing: it waits for a response of the recorded length.
    fixed_output_lengths: bool = False
    # A closed replay's throughput over its steady part only, while every
    # concurrency slot is busy (LLMBench's steady_state_only): the ramp-down at
    # the end, where one long request can hold the run open, is left out.
    steady_state_only: bool = False
    warmup_s: int = Field(default=30, ge=0, le=3600)
    max_lag_s: float = Field(default=2.0, gt=0, le=60)
    bfcl: BFCLSpec | None = None

    @field_validator("concurrencies")
    @classmethod
    def _levels(cls, v: list[int]) -> list[int]:
        levels = sorted({int(c) for c in v if int(c) > 0})
        if not levels:
            raise ValueError("at least one concurrency level")
        if len(levels) > 12:
            raise ValueError("at most 12 concurrency levels")
        return levels

    @model_validator(mode="after")
    def _one_dataset(self) -> Self:
        if self.dataset_profile.strip() and self.dataset_frozen.strip():
            raise ValueError("a replay names a collection profile or a frozen dataset, not both")
        if self.steady_state_only and (self.kind != "replay" or self.schedule != "closed"):
            raise ValueError(
                "steady_state_only applies to a closed replay (fixed concurrency) only"
            )
        if self.kind != "sweep" and (self.mode != "grid" or self.slo is not None
                                     or self.report_level != "slo_best" or self.fixed_level):
            raise ValueError("mode, slo, report_level and fixed_level are a sweep's")
        if self.mode == "auto" and (self.report_level != "slo_best" or self.fixed_level):
            raise ValueError("report_level and fixed_level are grid mode's: "
                             "auto reports the level its search confirms")
        if self.report_level == "fixed" and self.fixed_level not in self.concurrencies:
            raise ValueError("report_level fixed needs fixed_level, one of the concurrencies")
        if self.kind == "bfcl" and self.bfcl is None:
            self.bfcl = BFCLSpec()
        if self.kind != "bfcl" and self.bfcl is not None:
            raise ValueError(
                "bfcl parameters require kind=bfcl; use bfcl_quality for a quality check")
        return self

    def essentials(self) -> dict:
        """Only what this kind actually uses, so two specs that differ in an
        unused field are the same benchmark."""
        if self.kind == "sweep":
            out = {"kind": "sweep", "input_tokens": self.input_tokens,
                   "output_tokens": self.output_tokens, "concurrencies": self.concurrencies,
                   "requests_per_concurrency": self.requests_per_concurrency,
                   "max_seconds_per_level": self.max_seconds_per_level}
            # Only when set, so every sweep spec written before keeps its slug.
            if self.mode == "auto":
                del out["concurrencies"]
                out.update(mode="auto", max_concurrency=self.max_concurrency,
                           probe_requests_per_concurrency=self.probe_requests_per_concurrency,
                           probe_seconds=self.probe_seconds, granularity=self.granularity,
                           max_total_seconds=self.max_total_seconds)
            if self.slo is not None:
                out["slo"] = self.slo.model_dump()
            if self.report_level != "slo_best":
                out["report_level"] = self.report_level
            if self.fixed_level:
                out["fixed_level"] = self.fixed_level
        elif self.kind == "bfcl":
            out = {"kind": "bfcl", "bfcl": self.bfcl.model_dump(exclude_none=True)}
        elif self.schedule == "timed":
            out = {"kind": "replay", "dataset_profile": self.dataset_profile.strip(),
                   "schedule": "timed", "warmup_s": self.warmup_s,
                   "max_lag_s": self.max_lag_s}
            if self.dataset_frozen.strip():
                out["dataset_frozen"] = self.dataset_frozen.strip()
            # Only when set, so every timed spec written before keeps its slug.
            if self.fixed_output_lengths:
                out["fixed_output_lengths"] = True
        else:
            out = {"kind": "replay", "dataset_profile": self.dataset_profile.strip(),
                   "requests": self.requests, "concurrency": self.concurrency}
            # Only when set, so every replay spec written before keeps its slug.
            if self.dataset_frozen.strip():
                out["dataset_frozen"] = self.dataset_frozen.strip()
            if self.fixed_output_lengths:
                out["fixed_output_lengths"] = True
            if self.steady_state_only:
                out["steady_state_only"] = True
        return out

    @property
    def description(self) -> str:
        """The workload in a few words, for a benchmark's title."""
        if self.kind == "sweep":
            if self.mode == "auto":
                return (f"sweep — {self.input_tokens} in / {self.output_tokens} out, "
                        f"searching up to concurrency {self.max_concurrency} for the SLO")
            levels = ",".join(str(c) for c in self.concurrencies)
            return (f"sweep — {self.input_tokens} in / {self.output_tokens} out "
                    f"at concurrency {levels}, {self.requests_per_concurrency} requests "
                    "per slot")
        if self.kind == "bfcl":
            if self.bfcl.concurrency_grid:
                levels = ",".join(str(c) for c in self.bfcl.concurrency_grid)
                conc = f"at case concurrency grid {levels}"
            else:
                conc = f"at case concurrency {self.bfcl.concurrency}"
            return (
                f"BFCL — {self.bfcl.task_set}, {self.bfcl.cases_per_category} "
                f"cases/category {conc}"
            )
        dataset = (self.dataset_profile.strip() or self.dataset_frozen.strip()
                   or "the example set")
        fixed = ", responses at their recorded lengths" if self.fixed_output_lengths else ""
        if self.schedule == "timed":
            return f"timed replay — {dataset}, on its recorded timing (1x load){fixed}"
        count = f"{self.requests} requests" if self.requests else "all requests"
        steady = ", throughput over the steady part" if self.steady_state_only else ""
        return f"replay — {dataset}, {count} at concurrency {self.concurrency}{fixed}{steady}"

    @property
    def module(self) -> str:
        """The module this workload runs — and so the namespace of its metrics."""
        if self.kind == "bfcl":
            return BFCL_MODULE
        return SWEEP_MODULE if self.kind == "sweep" else replay_module()


# A benchmark that runs for a night helps nobody: this many workloads after
# the first is enough for a replay, a sweep and a second look at either.
MAX_FURTHER_WORKLOADS = 4


class BenchmarkSpec(Workload):
    """What every candidate is measured with: a workload (see Workload), and
    optionally

    - `more`: further workloads, run after it in the order given, in the same
      benchmark and on the same engine — a replay and then a sweep, or a sweep
      at two prompt lengths. The first workload stays the one a benchmark is
      known by: its kind names the slug and files the benchmark, and its
      module keeps the bare metric names, while a second run of the same
      module reports under `<module>#2` (app/evaluation/llmbench.py). A
      further replay names a frozen dataset: a rolling profile is pinned per
      campaign, for its first workload only.
    - `quality`: a quality suite after the workloads.
    """

    more: list[Workload] = Field(default_factory=list, max_length=MAX_FURTHER_WORKLOADS)
    quality: QualitySpec | None = None
    bfcl_quality: BFCLSpec | None = None

    @model_validator(mode="after")
    def _further_replays_are_frozen(self) -> Self:
        if self.kind == "bfcl" and self.bfcl_quality is not None:
            raise ValueError("a BFCL workload already measures tool-use quality")
        for workload in self.more:
            if workload.kind == "replay" and workload.dataset_profile.strip():
                raise ValueError(
                    "a further replay names a frozen dataset (dataset_frozen): a rolling "
                    "profile is pinned for a benchmark's first workload only"
                )
        return self

    def require_ranking_metric(self, target: str) -> None:
        """Ask LLMBench to explain missing token usage when this stage ranks by BFCL TPM."""
        bfcl = self.bfcl or self.bfcl_quality
        if bfcl is None:
            return
        targets = {"bfcl_v4.total_tpm", "bfcl_v4/total_tpm", "bfcl_v4/total_tpm_per_gpu"}
        if self.kind == "bfcl":
            targets.update(("", "total_tpm", "total_tpm_per_gpu"))
        if target in targets and "total_tpm" not in bfcl.required_metrics:
            bfcl.required_metrics.append("total_tpm")

    def essentials(self) -> dict:
        """Only what is used, so two specs that differ in an unused field are
        the same benchmark."""
        out = super().essentials()
        # Only when set, so every spec written before keeps its slug.
        if self.more:
            out["more"] = [workload.essentials() for workload in self.more]
        # Likewise only when set.
        if self.quality is not None:
            # "once": a benchmark made before each question was answered once
            # (avg@8 for GPQA) is not this one, and LLMBench keeps a slug's
            # document as first created.
            out["quality"] = {**self.quality.model_dump(), "once": True}
        if self.bfcl_quality is not None:
            out["bfcl_quality"] = self.bfcl_quality.model_dump(exclude_none=True)
        return out

    @property
    def slug(self) -> str:
        digest = hashlib.sha256(
            json.dumps(self.essentials(), sort_keys=True).encode()
        ).hexdigest()[:8]
        return f"autotune-{self.kind}-{digest}"

    @property
    def title(self) -> str:
        title = "AutoTune " + "; then ".join(w.description for w in self.workloads)
        if self.quality is not None:
            q = self.quality
            each = f"{q.questions} questions each" if q.questions else "every question"
            title += f" + quality ({', '.join(q.tasks)}, {each})"
        if self.bfcl_quality is not None:
            title += f" + BFCL quality ({self.bfcl_quality.task_set})"
        return title

    @property
    def workloads(self) -> list[Workload]:
        """Every workload, in the order it runs."""
        return [self, *self.more]

    @property
    def modules(self) -> list[str]:
        """The key of each module run, in order: what its metrics are
        reported under (`perf_guidellm_sweep`, `perf_guidellm_sweep#2`, …)."""
        names = [w.module for w in self.workloads]
        if self.quality is not None:
            names.append(QUALITY_MODULE)
        if self.bfcl_quality is not None:
            names.append(BFCL_MODULE)
        return instance_keys(names)

    @property
    def group_tag(self) -> str:
        return f"{GROUP_TAG}/{self.kind}"


def metric_problem(name: str, spec: BenchmarkSpec) -> str | None:
    """Why a benchmark built from `spec` can never report `name`, or None
    when it can. `name` is a measure (`output_tpm_per_gpu`, `ttft_ms@p99`) or
    a raw key (`perf_guidellm_sweep#2.output_tps`), whose module must be one
    the benchmark runs."""
    from app.measures import problem_with
    from app.measures.refs import is_measure_ref

    if is_measure_ref(name):
        return problem_with(name, [w.module for w in spec.workloads]
                            + ([QUALITY_MODULE] if spec.quality is not None else [])
                            + ([BFCL_MODULE] if spec.bfcl_quality is not None else []))
    if name == "score_total":
        return None
    module = name.split(".", 1)[0]
    if "." not in name or module not in spec.modules:
        return f"{name}: no module {module} in this benchmark (it runs {', '.join(spec.modules)})"
    return None


def replay_module() -> str:
    return get_settings().llmbench_replay_module or "replay"


def workload_module(spec: Workload, client: LLMBenchClient | None = None) -> dict:
    """The LLMBench module that runs one workload."""
    if spec.kind == "sweep":
        params: dict = {
            "search_mode": spec.mode,
            "input_tokens": spec.input_tokens,
            "output_tokens": spec.output_tokens,
            "requests_per_concurrency": spec.requests_per_concurrency,
            "max_seconds": spec.max_seconds_per_level,
            "warmup_seconds": 10,
            "request_timeout": 300,
        }
        if spec.mode == "auto":
            params.update(search_max_concurrency=spec.max_concurrency,
                          probe_requests_per_concurrency=spec.probe_requests_per_concurrency,
                          probe_seconds=spec.probe_seconds,
                          search_granularity=spec.granularity,
                          max_total_seconds=spec.max_total_seconds)
        else:
            params["concurrencies"] = ",".join(str(c) for c in spec.concurrencies)
            if spec.report_level != "slo_best":
                params["report_level"] = spec.report_level
            if spec.fixed_level:
                params["fixed_level"] = spec.fixed_level
        if spec.slo is not None:
            params.update(slo_max_ttft_ms=spec.slo.ttft_ms,
                          slo_ttft_percentile=spec.slo.ttft_percentile,
                          slo_min_request_output_tps=spec.slo.min_request_output_tps)
        return {"module_name": SWEEP_MODULE, "params": params, "metric_configs": _SWEEP_METRICS}
    if spec.kind == "bfcl":
        return bfcl_module(spec.bfcl)
    if spec.schedule == "timed":
        params: dict = {"schedule": "timed", "warmup_s": spec.warmup_s,
                        "max_lag_s": spec.max_lag_s}
        if spec.fixed_output_lengths:
            params["fixed_output_lengths"] = True
    else:
        params = {"concurrency": spec.concurrency, "max_samples": spec.requests}
        if spec.fixed_output_lengths:
            params["fixed_output_lengths"] = True
        if spec.steady_state_only:
            params["steady_state_only"] = True
    if spec.dataset_profile.strip():
        params.update(dataset_source="auto", dataset_profile=spec.dataset_profile.strip())
    elif spec.dataset_frozen.strip():
        params.update(dataset_source="fixed",
                      dataset_path=frozen_path(spec.dataset_frozen.strip(), client))
    return {
        "module_name": replay_module(),
        "params": params,
        "metric_configs": _replay_metrics(client),
    }


def document(spec: BenchmarkSpec, client: LLMBenchClient | None = None) -> dict:
    """The LLMBench export-format document for `spec`."""
    # LLMBench's own score is the first workload's: AutoTune ranks on its
    # objective, applied to the raw metrics of whichever module it names.
    modules = [
        {**workload_module(workload, client), "order_index": index,
         "weight": 1.0 if index == 0 else 0.0, "skip_if_prev_failed": False}
        for index, workload in enumerate(spec.workloads)
    ]
    if spec.quality is not None:
        modules.append({**quality_module(spec.quality), "order_index": len(modules),
                        "weight": 0.0, "skip_if_prev_failed": False})
    if spec.bfcl_quality is not None:
        modules.append({**bfcl_module(spec.bfcl_quality, quality=True),
                        "order_index": len(modules), "weight": 0.0,
                        "skip_if_prev_failed": False})
    return {
        "slug": spec.slug,
        "name": spec.title,
        "description": "Created and locked by LLM AutoTune from a campaign's workload. "
                       "The same workload always maps to this benchmark.",
        "version": "1",
        "status": "active",
        "group_tags": [spec.group_tag],
        "modules": modules,
    }


def quality_module(quality: QualitySpec) -> dict:
    """The opencompass module for `quality`. Each task answered once (k = 1),
    which the module runs greedily (temperature 0) unless forced otherwise: a
    score that moves only when the config does. Scores are display-only in
    LLMBench; AutoTune judges them against production's."""
    params: dict = {
        "selected_benchmarks": list(quality.tasks),
        "max_tokens": quality.max_tokens,
        "max_workers": quality.workers,
        "max_seconds": quality.max_seconds,
        "request_timeout": 1800,
        "force_temperature": False,
        "stream": True,
    }
    for task in quality.tasks:
        suffix = QUALITY_TASKS[task]
        params[f"sample_cap_{suffix}"] = quality.questions
        # The module's own defaults sample some suites many times (GPQA avg@8,
        # AIME avg@32): eight to thirty-two times the requests, and at a
        # sampling temperature. Once each is what this check is.
        params[f"avg_k_{suffix}"] = 1
        params[f"pass_k_{suffix}"] = 1
    return {
        "module_name": QUALITY_MODULE,
        "params": params,
        "metric_configs": [
            {"key": task, "role": "display", "weight": 1, "formula": "passthrough"}
            for task in quality.tasks
        ],
    }


def frozen_path(name: str, client: LLMBenchClient | None) -> str:
    """Where LLMBench keeps the frozen dataset `name`: what a `fixed` replay
    module reads. Refused (BenchmarkRefused) when LLMBench has no such dataset."""
    from app.datasets.profiles import DatasetProfileClient

    for dataset in DatasetProfileClient(client).frozen():
        if dataset.get("name") == name and dataset.get("path"):
            return str(dataset["path"])
    raise BenchmarkRefused(f"LLMBench has no frozen dataset named {name}")


def _replay_metrics(client: LLMBenchClient | None) -> list[dict]:
    """The replay module's own default evaluation, as LLMBench describes it."""
    if client is not None:
        try:
            configs = client.module_defaults(replay_module()).get("default_metric_configs")
            if configs:
                return configs
        except Exception:
            pass
    return _REPLAY_FALLBACK_METRICS


def ensure_spec(spec: BenchmarkSpec, client: LLMBenchClient | None = None) -> Ensured:
    """Create (once) and lock the benchmark for `spec`; return its slug."""
    client = client or LLMBenchClient()
    return ensure_document(client, document(spec, client), spec.slug)
