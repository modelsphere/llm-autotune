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
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.config import get_settings
from app.evaluation.benchmarks import GROUP_TAG, BenchmarkRefused, Ensured, ensure_document
from app.evaluation.llmbench import LLMBenchClient

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


class BenchmarkSpec(BaseModel):
    """What every candidate is measured with. Two kinds:

    - `sweep`: random prompts of `input_tokens`, answers of `output_tokens`,
      at each of `concurrencies`. Level c sends c × `requests_per_concurrency`
      requests, so every level is measured on the same sample per slot however
      fast the engine is; `max_seconds_per_level` caps a slow one. Fast, and
      blind to prefix-cache reuse.
    - `replay`: requests recorded from real traffic, replayed `concurrency` at
      a time. `dataset_profile` names an LLMBench collection profile (a rolling
      sample), or `dataset_frozen` a frozen dataset (a file that never
      changes); neither replays LLMBench's built-in example set (proves the
      pipeline, measures nothing). `requests` caps how many (0 = the whole
      dataset).

    Either kind may add `quality`: a quality suite after the workload, in the
    same benchmark.
    """

    kind: Literal["sweep", "replay"] = "sweep"
    input_tokens: int = Field(default=2048, ge=1, le=1_000_000)
    output_tokens: int = Field(default=512, ge=1, le=100_000)
    concurrencies: list[int] = Field(default_factory=lambda: [1, 4, 16, 64])
    requests_per_concurrency: int = Field(default=20, ge=1, le=10_000)
    max_seconds_per_level: int = Field(default=600, ge=10, le=7200)
    dataset_profile: str = ""
    dataset_frozen: str = ""
    requests: int = Field(default=500, ge=0, le=1_000_000)
    concurrency: int = Field(default=16, ge=1, le=4096)
    quality: QualitySpec | None = None

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
    def _one_dataset(self) -> BenchmarkSpec:
        if self.dataset_profile.strip() and self.dataset_frozen.strip():
            raise ValueError("a replay names a collection profile or a frozen dataset, not both")
        return self

    def essentials(self) -> dict:
        """Only what this kind actually uses, so two specs that differ in an
        unused field are the same benchmark."""
        if self.kind == "sweep":
            out = {"kind": "sweep", "input_tokens": self.input_tokens,
                   "output_tokens": self.output_tokens, "concurrencies": self.concurrencies,
                   "requests_per_concurrency": self.requests_per_concurrency,
                   "max_seconds_per_level": self.max_seconds_per_level}
        else:
            out = {"kind": "replay", "dataset_profile": self.dataset_profile.strip(),
                   "requests": self.requests, "concurrency": self.concurrency}
            # Only when set, so every replay spec written before keeps its slug.
            if self.dataset_frozen.strip():
                out["dataset_frozen"] = self.dataset_frozen.strip()
        # Likewise only when set.
        if self.quality is not None:
            # "once": a benchmark made before each question was answered once
            # (avg@8 for GPQA) is not this one, and LLMBench keeps a slug's
            # document as first created.
            out["quality"] = {**self.quality.model_dump(), "once": True}
        return out

    @property
    def slug(self) -> str:
        digest = hashlib.sha256(
            json.dumps(self.essentials(), sort_keys=True).encode()
        ).hexdigest()[:8]
        return f"autotune-{self.kind}-{digest}"

    @property
    def title(self) -> str:
        if self.kind == "sweep":
            levels = ",".join(str(c) for c in self.concurrencies)
            title = (f"AutoTune sweep — {self.input_tokens} in / {self.output_tokens} out "
                     f"at concurrency {levels}, {self.requests_per_concurrency} requests "
                     "per slot")
        else:
            dataset = (self.dataset_profile.strip() or self.dataset_frozen.strip()
                       or "the example set")
            count = f"{self.requests} requests" if self.requests else "all requests"
            title = f"AutoTune replay — {dataset}, {count} at concurrency {self.concurrency}"
        if self.quality is not None:
            q = self.quality
            each = f"{q.questions} questions each" if q.questions else "every question"
            title += f" + quality ({', '.join(q.tasks)}, {each})"
        return title

    @property
    def module(self) -> str:
        """The module this spec runs — and so the namespace of its metrics."""
        return SWEEP_MODULE if self.kind == "sweep" else replay_module()

    @property
    def group_tag(self) -> str:
        return f"{GROUP_TAG}/{self.kind}"


def replay_module() -> str:
    return get_settings().llmbench_replay_module or "replay"


def document(spec: BenchmarkSpec, client: LLMBenchClient | None = None) -> dict:
    """The LLMBench export-format document for `spec`."""
    if spec.kind == "sweep":
        module = {
            "module_name": SWEEP_MODULE,
            "params": {
                "search_mode": "grid",
                "concurrencies": ",".join(str(c) for c in spec.concurrencies),
                "input_tokens": spec.input_tokens,
                "output_tokens": spec.output_tokens,
                "requests_per_concurrency": spec.requests_per_concurrency,
                "max_seconds": spec.max_seconds_per_level,
                "warmup_seconds": 10,
                "request_timeout": 300,
            },
            "metric_configs": _SWEEP_METRICS,
        }
    else:
        params: dict = {"concurrency": spec.concurrency, "max_samples": spec.requests}
        if spec.dataset_profile.strip():
            params.update(dataset_source="auto", dataset_profile=spec.dataset_profile.strip())
        elif spec.dataset_frozen.strip():
            params.update(dataset_source="fixed",
                          dataset_path=frozen_path(spec.dataset_frozen.strip(), client))
        module = {
            "module_name": replay_module(),
            "params": params,
            "metric_configs": _replay_metrics(client),
        }
    modules = [{**module, "order_index": 0, "weight": 1.0, "skip_if_prev_failed": False}]
    if spec.quality is not None:
        modules.append({**quality_module(spec.quality), "order_index": 1, "weight": 0.0,
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
