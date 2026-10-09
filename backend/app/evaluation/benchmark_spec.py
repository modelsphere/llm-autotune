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

from pydantic import BaseModel, Field, field_validator

from app.core.config import get_settings
from app.evaluation.benchmarks import GROUP_TAG, Ensured, ensure_document
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


class BenchmarkSpec(BaseModel):
    """What every candidate is measured with. Two kinds:

    - `sweep`: random prompts of `input_tokens`, answers of `output_tokens`,
      at each of `concurrencies`, `seconds_per_level` each. Fast, and blind to
      prefix-cache reuse.
    - `replay`: requests recorded from real traffic, replayed `concurrency` at
      a time. `dataset_profile` names an LLMBench collection profile; empty
      replays LLMBench's built-in example set (proves the pipeline, measures
      nothing). `requests` caps how many (0 = the whole dataset).
    """

    kind: Literal["sweep", "replay"] = "sweep"
    input_tokens: int = Field(default=2048, ge=1, le=1_000_000)
    output_tokens: int = Field(default=512, ge=1, le=100_000)
    concurrencies: list[int] = Field(default_factory=lambda: [1, 4, 16, 64])
    seconds_per_level: int = Field(default=60, ge=10, le=3600)
    dataset_profile: str = ""
    requests: int = Field(default=500, ge=0, le=1_000_000)
    concurrency: int = Field(default=16, ge=1, le=4096)

    @field_validator("concurrencies")
    @classmethod
    def _levels(cls, v: list[int]) -> list[int]:
        levels = sorted({int(c) for c in v if int(c) > 0})
        if not levels:
            raise ValueError("at least one concurrency level")
        if len(levels) > 12:
            raise ValueError("at most 12 concurrency levels")
        return levels

    def essentials(self) -> dict:
        """Only what this kind actually uses, so two specs that differ in an
        unused field are the same benchmark."""
        if self.kind == "sweep":
            return {"kind": "sweep", "input_tokens": self.input_tokens,
                    "output_tokens": self.output_tokens, "concurrencies": self.concurrencies,
                    "seconds_per_level": self.seconds_per_level}
        return {"kind": "replay", "dataset_profile": self.dataset_profile.strip(),
                "requests": self.requests, "concurrency": self.concurrency}

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
            return (f"AutoTune sweep — {self.input_tokens} in / {self.output_tokens} out "
                    f"at concurrency {levels}")
        dataset = self.dataset_profile.strip() or "the example set"
        count = f"{self.requests} requests" if self.requests else "all requests"
        return f"AutoTune replay — {dataset}, {count} at concurrency {self.concurrency}"

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
                "max_seconds": spec.seconds_per_level,
                "warmup_seconds": 10,
                "request_timeout": 300,
            },
            "metric_configs": _SWEEP_METRICS,
        }
    else:
        params: dict = {"concurrency": spec.concurrency, "max_samples": spec.requests}
        if spec.dataset_profile.strip():
            params.update(dataset_source="auto", dataset_profile=spec.dataset_profile.strip())
        module = {
            "module_name": replay_module(),
            "params": params,
            "metric_configs": _replay_metrics(client),
        }
    return {
        "slug": spec.slug,
        "name": spec.title,
        "description": "Created and locked by LLM AutoTune from a campaign's workload. "
                       "The same workload always maps to this benchmark.",
        "version": "1",
        "status": "active",
        "group_tags": [spec.group_tag],
        "modules": [{**module, "order_index": 0, "weight": 1.0,
                     "skip_if_prev_failed": False}],
    }


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
