"""What a benchmark sends, said in measure terms, for whoever reads its results.

A policy that is told only a benchmark slug has to guess what the numbers
mean: that a replay runs at one fixed concurrency (so "the level it settled
on" means nothing), that a sweep picks its operating point by an SLO, how
long the prompts are. This lifts that from the benchmark's own locked module
params — a whitelist of the ones that shape the results, never a credential.
"""

from __future__ import annotations

from typing import Any

from app.measures.adapters import FAMILIES, family_of

# Per family: the params that decide what the numbers describe.
_PARAMS: dict[str, tuple[str, ...]] = {
    "bfcl_v4": (
        "task_set", "cases_per_category", "concurrency", "concurrency_grid", "max_seconds",
        "max_total_seconds",
        "request_timeout_seconds", "request_retries", "probe_endpoint", "required_metrics",
        "min_ttft_coverage", "min_usage_coverage", "min_cache_usage_coverage",
    ),
    "perf_guidellm_sweep": (
        "search_mode", "concurrencies", "input_tokens", "output_tokens", "max_seconds",
        "requests_per_concurrency", "slo_min_request_output_tps", "slo_max_ttft_ms",
        "slo_ttft_percentile", "search_max_concurrency",
    ),
    "perf_guidellm": ("concurrency", "input_tokens", "output_tokens", "max_seconds"),
    "replay": (
        "concurrency", "max_samples", "max_seconds", "max_generation_tokens",
        "dataset_profile", "judge_enable",
    ),
}

_SAYS: dict[str, str] = {
    "bfcl_v4": (
        "Frozen official BFCL subset with native streamed tool calls. Concurrency counts "
        "active cases, not HTTP calls. Accuracy is official case correctness; "
        "passed cases/hour and token rates use generation time, excluding grading. "
        "Compare identical task sets, case counts, concurrency and retry policies."
    ),
    "perf_guidellm_sweep": (
        "Synthetic prompts at several concurrency levels. Its measures describe the "
        "level with the highest total throughput that met the sweep's SLO "
        "(per-request output speed floor and TTFT cap); every level is also "
        "addressable (`:c8`)."
    ),
    "perf_guidellm": "Synthetic prompts at one concurrency; its measures describe that run.",
    "replay": (
        "Recorded production requests, replayed at one fixed concurrency: there are no "
        "levels and nothing to settle on. Its measures describe the whole replay; "
        "prompts are as long and as cache-friendly as real traffic."
    ),
}


def describe(modules: list[dict[str, Any]], aliases: dict[str, str] | None = None,
             replay_names: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """One entry per module of a benchmark (LLMBench's `modules` list, each
    with `module_name`, `params_json`, `order_index`), in benchmark order."""
    from app.evaluation.llmbench import instance_keys

    ordered = sorted((m for m in modules or [] if isinstance(m, dict)),
                     key=lambda m: m.get("order_index") or 0)
    keys = instance_keys(str(m.get("module_name") or "") for m in ordered)
    out = []
    for key, module in zip(keys, ordered, strict=True):
        name = str(module.get("module_name") or "")
        family = family_of(name, replay_names, aliases)
        # LLMBench lists a module's params as `params_json`; `params` is the
        # spelling of a benchmark document being written.
        params = module.get("params_json") or module.get("params") or {}
        wanted = _PARAMS.get(family, ())
        entry: dict[str, Any] = {
            "scenario": key,
            "module": name,
            "family": family if family in FAMILIES else "unknown",
            "params": {k: params[k] for k in wanted if k in params},
        }
        if family in _SAYS:
            entry["reads_as"] = _SAYS[family]
        out.append(entry)
    return out
