"""Translating each LLMBench module's raw keys into measures.

One adapter per module family, written against the module's source:

    perf_guidellm_sweep   a concurrency sweep: one set of numbers per level
    perf_guidellm         guidellm at one level
    replay                recorded traffic at one concurrency
    agentic, opencompass, functional_acceptance, tool_call_success,
    hallucination, case_truncation        what was answered, and how well

A module this file does not know still yields its `score`, and its raw keys
stay addressable — a new LLMBench module lands as data before it lands here.
A build of LLMBench that names a module otherwise maps the name onto a family
(`AUTOTUNE_LLMBENCH_MODULE_FAMILIES`, or the replay's own setting).

Every adapter declares what it CAN report (`supports`). That is what lets an
objective be refused when it is written ("this benchmark never reports
ttft_ms@p95") instead of every run scoring None after a whole night.

The operating point
-------------------
A scenario's measures describe one operating point unless a reference names
another (`:c8`, `:peak`):

- a sweep (grid mode): the level with the highest total throughput among the
  levels that met the sweep's SLO. When no level met it, the lowest level,
  with `meets_slo` 0 — the same fallback LLMBench reports. A sweep from an
  LLMBench too old to flag each level falls back to the level it reported.
- a sweep (auto mode): the level the search confirmed at full length. Its
  other levels are short probes, too noisy to choose by.
- one-level modules and replays: the run itself.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from app.measures.catalog import PROMPT_BUCKETS

# LLMBench normalizes `*_card_norm` rates to this many cards.
CARD_NORM_BASELINE = 8

_LEVEL = re.compile(r"^c(\d+)$")
_LAT = {"mean": "mean", "p50": "p50", "p90": "p90", "p99": "p99"}


def number(value: Any) -> float | None:
    """A finite float, or None for anything that is not a measured number —
    including the bool a flag arrives as, which is read with `flag`."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def flag(value: Any) -> float | None:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    n = number(value)
    return None if n is None else (1.0 if n else 0.0)


@dataclass
class Scenario:
    """What one module run measured, as measures."""

    key: str  # the module's instance key in a run's metrics: `replay`, `perf_guidellm_sweep#2`
    module: str  # the module's name without the instance suffix
    family: str
    kind: str  # sweep | single | replay | quality
    # Measures per point: "operating" always (possibly empty), plus "peak" and
    # each "c<N>" for a sweep.
    points: dict[str, dict[str, float]] = field(default_factory=dict)
    # Which level the operating point is (`c16`), for a sweep.
    operating_level: str = ""
    # How the operating point was chosen, in words, for a report or an agent.
    operating_rule: str = ""
    supports: frozenset[str] = frozenset()

    @property
    def operating(self) -> dict[str, float]:
        return self.points.get("operating", {})

    @property
    def is_throughput(self) -> bool:
        return self.kind in ("sweep", "single", "replay")


def _put(out: dict[str, float], key: str, value: Any) -> None:
    n = number(value)
    if n is not None:
        out[key] = n


def _ratio(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return a / b


def _per_gpu(out: dict[str, float], cards: float | None) -> None:
    """The per-GPU form of every deployment rate this point has."""
    if not cards:
        return
    for rate in ("output_tpm", "input_tpm", "uncached_input_tpm", "cached_input_tpm", "total_tpm"):
        if rate in out:
            out[f"{rate}_per_gpu"] = out[rate] / cards


def _health_from_uptime(
    out: dict[str, float], uptime: float | None, measured: float | None
) -> None:
    """guidellm's uptime is 1 − errored/total over the requests it measured."""
    if uptime is None:
        return
    out["success_rate"] = uptime
    out["error_rate"] = 1.0 - uptime
    if measured is not None:
        out["requests_total"] = measured
        out["requests_completed"] = round(measured * uptime)
        out["requests_failed"] = measured - out["requests_completed"]


# -- guidellm, one level ----------------------------------------------------------

GUIDELLM_LEVEL_SUPPORTS = frozenset(
    {
        "output_tpm",
        "input_tpm",
        "total_tpm",
        "output_tpm_per_gpu",
        "input_tpm_per_gpu",
        "total_tpm_per_gpu",
        "request_rate",
        "request_output_tps@mean",
        *(f"{m}@{s}" for m in ("ttft_ms", "itl_ms", "tpot_ms") for s in _LAT),
        "success_rate",
        "error_rate",
        "requests_total",
        "requests_completed",
        "requests_failed",
        "concurrency",
        "window_s",
        "prompt_tokens_mean",
        "completion_tokens_mean",
    }
)


def _guidellm_level(
    raw: dict[str, Any],
    concurrency: float | None,
    cards: float | None,
    params: dict[str, Any],
    *,
    aggregate_out: str,
    aggregate_in: str,
) -> dict[str, float]:
    """One guidellm level's numbers as measures.

    `output_tps_mean`/`total_tps_mean` are AGGREGATE rates of the level here.
    Input is total − output: guidellm's own input rate is a mean over each
    request's prefill window, which counts overlapping requests' seconds more
    than once, while total and output are tokens over wall clock.
    """
    out: dict[str, float] = {}
    output_tps = number(raw.get(aggregate_out))
    total_tps = number(raw.get("total_tps_mean"))
    input_tps = number(raw.get(aggregate_in))
    if output_tps is not None:
        out["output_tpm"] = output_tps * 60
    if total_tps is not None:
        out["total_tpm"] = total_tps * 60
        if output_tps is not None:
            out["input_tpm"] = max(total_tps - output_tps, 0.0) * 60
    elif input_tps is not None and output_tps is not None:
        # A module that reports no total adds the two.
        out["input_tpm"] = input_tps * 60
        out["total_tpm"] = (input_tps + output_tps) * 60
    _per_gpu(out, cards)

    request_tps = number(raw.get("request_output_tps"))
    if request_tps is None and output_tps is not None and concurrency:
        request_tps = output_tps / concurrency
    _put(out, "request_output_tps@mean", request_tps)
    for measure in ("ttft", "itl", "tpot"):
        for stat in ("p50", "p90", "p99"):
            _put(out, f"{measure}_ms@{stat}", raw.get(f"{measure}_{stat}_ms"))
        _put(out, f"{measure}_ms@mean", raw.get(f"{measure}_mean_ms"))

    measured = number(raw.get("measured_requests")) or number(raw.get("request_count"))
    _health_from_uptime(out, number(raw.get("uptime")), measured)
    window = number(raw.get("duration_seconds"))
    _put(out, "window_s", window)
    if "requests_completed" in out and window:
        out["request_rate"] = out["requests_completed"] / window
    _put(out, "concurrency", concurrency)
    _put(out, "prompt_tokens_mean", params.get("input_tokens"))
    _put(out, "completion_tokens_mean", params.get("output_tokens"))
    return out


def _sweep(sc: Scenario, raw: dict[str, Any], params: dict[str, Any], cards: float | None) -> None:
    levels: dict[int, dict[str, Any]] = {}
    for key, value in raw.items():
        head, _, leaf = key.partition(".")
        match = _LEVEL.match(head)
        if match and leaf:
            levels.setdefault(int(match.group(1)), {})[leaf] = value
    flagged = {c: flag(lv.get("meets_slo")) for c, lv in levels.items()}
    for c, lv in sorted(levels.items()):
        point = _guidellm_level(
            lv,
            float(c),
            cards,
            params,
            aggregate_out="output_tps_mean",
            aggregate_in="input_tps_mean",
        )
        if flagged[c] is not None:
            point["meets_slo"] = flagged[c]
        sc.points[f"c{c}"] = point

    def total(c: int) -> float:
        return number(levels[c].get("total_tps_mean")) or -1.0

    chosen: int | None = None
    reported = number(raw.get("reported_concurrency"))
    if levels:
        sc.points["peak"] = sc.points[f"c{max(levels, key=total)}"]
    if "search_probes" in raw and reported is not None and int(reported) in levels:
        chosen = int(reported)
        sc.operating_rule = "the level the auto search confirmed at full length"
    elif any(v is not None for v in flagged.values()):
        passing = [c for c, ok in flagged.items() if ok]
        if passing:
            chosen = max(passing, key=lambda c: (total(c), c))
            sc.operating_rule = "the level with the highest total throughput that met the SLO"
        else:
            chosen = min(levels)
            sc.operating_rule = "no level met the SLO; the lowest level, as LLMBench reports it"
    elif reported is not None and int(reported) in levels:
        chosen = int(reported)
        sc.operating_rule = (
            "the level LLMBench reported (this result flags no level against the SLO)"
        )
    if chosen is not None:
        sc.operating_level = f"c{chosen}"
        point = dict(sc.points[f"c{chosen}"])
        if "meets_slo" not in point and number(raw.get("reported_level_meets_slo")) is not None:
            point["meets_slo"] = flag(raw.get("reported_level_meets_slo"))
        sc.points["operating"] = point
    sc.supports = GUIDELLM_LEVEL_SUPPORTS | {"meets_slo"}


def _single(sc: Scenario, raw: dict[str, Any], params: dict[str, Any], cards: float | None) -> None:
    concurrency = number(raw.get("reported_concurrency")) or number(params.get("concurrency"))
    sc.points["operating"] = _guidellm_level(
        raw,
        concurrency,
        cards,
        params,
        # guidellm reports the level's mean rate; a simpler one-level test
        # only the plain aggregate rate.
        aggregate_out="output_tps_mean" if "output_tps_mean" in raw else "output_tps",
        aggregate_in="input_tps_mean" if "input_tps_mean" in raw else "input_tps",
    )
    sc.operating_rule = "the one level the module ran"
    sc.supports = GUIDELLM_LEVEL_SUPPORTS


# -- replay -------------------------------------------------------------------------

REPLAY_SUPPORTS = frozenset(
    {
        "output_tpm",
        "input_tpm",
        "uncached_input_tpm",
        "cached_input_tpm",
        "total_tpm",
        "output_tpm_per_gpu",
        "input_tpm_per_gpu",
        "uncached_input_tpm_per_gpu",
        "cached_input_tpm_per_gpu",
        "total_tpm_per_gpu",
        "request_rate",
        *(f"request_output_tps@{s}" for s in ("mean", "p10", "p50", "p90")),
        *(f"request_prefill_tps@{s}" for s in ("mean", "p10", "p50", "p90")),
        *(f"ttft_ms@{s}" for s in _LAT),
        *(f"ttft_ms@{s}[{b}]" for s in ("mean", "p50", "p90") for b in PROMPT_BUCKETS),
        *(f"prompt_bucket_requests[{b}]" for b in PROMPT_BUCKETS),
        "e2e_latency_ms@p50",
        "e2e_latency_ms@p99",
        "success_rate",
        "error_rate",
        "unfinished_rate",
        "truncated_rate",
        "not_started_rate",
        "requests_total",
        "requests_completed",
        "requests_failed",
        "requests_not_started",
        "requests_unfinished",
        "requests_truncated",
        "requests_runaway",
        "concurrency",
        "window_s",
        "prompt_tokens_mean",
        "completion_tokens_mean",
        "cache_hit_rate",
        "answer_quality_rate",
        "hallucination_rate",
        "poor_answer_rate",
        "score",
        "score_per_gpu",
    }
)


def _replay(sc: Scenario, raw: dict[str, Any], params: dict[str, Any], cards: float | None) -> None:
    """A replay's `output_tps_*`/`input_tps_*` are PER REQUEST (completion ÷
    total time, prompt ÷ TTFT); its rates per minute are the aggregate ones."""
    out: dict[str, float] = {}
    _put(out, "output_tpm", raw.get("output_tpm"))
    _put(out, "input_tpm", raw.get("input_tpm"))
    _put(out, "cached_input_tpm", raw.get("cached_tpm"))
    _put(out, "uncached_input_tpm", raw.get("uncached_input_tpm"))
    if "input_tpm" in out and "output_tpm" in out:
        out["total_tpm"] = out["input_tpm"] + out["output_tpm"]
    _per_gpu(out, cards)

    _put(out, "request_output_tps@mean", raw.get("output_tps_mean", raw.get("output_tps_avg")))
    for stat in ("p10", "p50", "p90"):
        _put(out, f"request_output_tps@{stat}", raw.get(f"output_tps_{stat}"))
    for stat in ("mean", "p10", "p50", "p90"):
        _put(out, f"request_prefill_tps@{stat}", raw.get(f"input_tps_{stat}"))
    for stat in ("p50", "p90", "p99"):
        _put(out, f"ttft_ms@{stat}", raw.get(f"ttft_{stat}_ms"))
    _put(out, "ttft_ms@mean", raw.get("ttft_mean_ms"))
    for bucket in PROMPT_BUCKETS:
        _put(out, f"ttft_ms@p50[{bucket}]", raw.get(f"ttft_{bucket}_p50_ms"))
        _put(out, f"ttft_ms@p90[{bucket}]", raw.get(f"ttft_{bucket}_p90_ms"))
        _put(out, f"ttft_ms@mean[{bucket}]", raw.get(f"ttft_{bucket}_avg_ms"))
        _put(out, f"prompt_bucket_requests[{bucket}]", raw.get(f"ttft_{bucket}_count"))
    _put(out, "e2e_latency_ms@p50", raw.get("total_time_p50_ms"))
    _put(out, "e2e_latency_ms@p99", raw.get("total_time_p99_ms"))

    total = number(raw.get("total_requests"))
    completed = number(raw.get("successful_requests"))
    not_started = number(raw.get("not_started_requests"))
    truncated = number(raw.get("finish_reason_length_count"))
    _put(out, "success_rate", raw.get("uptime"))
    _put(out, "error_rate", raw.get("error_rate"))
    _put(out, "unfinished_rate", raw.get("unfinished_rate"))
    _put(out, "truncated_rate", _ratio(truncated, completed))
    _put(out, "not_started_rate", _ratio(not_started, total))
    _put(out, "requests_total", total)
    _put(out, "requests_completed", completed)
    _put(out, "requests_failed", raw.get("error_requests"))
    _put(out, "requests_not_started", not_started)
    _put(out, "requests_unfinished", raw.get("unfinished_requests"))
    _put(out, "requests_truncated", truncated)
    runaway = [number(raw.get(k)) for k in ("generation_capped_count", "repetitive_token_count")]
    if any(v is not None for v in runaway):
        out["requests_runaway"] = sum(v or 0.0 for v in runaway)

    window = number(raw.get("wall_time_s"))
    _put(out, "window_s", window)
    _put(out, "request_rate", _ratio(completed, window))
    _put(out, "concurrency", params.get("concurrency"))
    _put(out, "prompt_tokens_mean", raw.get("avg_prompt_tokens"))
    _put(out, "completion_tokens_mean", raw.get("avg_completion_tokens"))
    _put(out, "cache_hit_rate", raw.get("cache_hit_rate"))
    # Present only when the benchmark enabled its judge.
    _put(out, "answer_quality_rate", raw.get("judge_good_acc_rate"))
    _put(out, "hallucination_rate", raw.get("judge_halluc_clear_rate"))
    _put(out, "poor_answer_rate", raw.get("judge_poor_rate"))
    sc.points["operating"] = out
    sc.operating_rule = "the replay itself, at the concurrency it was run at"
    sc.supports = REPLAY_SUPPORTS


# -- what was answered ------------------------------------------------------------


def _agentic(out: dict[str, float], raw: dict[str, Any]) -> frozenset[str]:
    for measure in ("ttft", "itl"):
        for stat in ("mean", "p50", "p99"):
            _put(out, f"{measure}_ms@{stat}", raw.get(f"{measure}_{stat}_ms"))
    total = number(raw.get("total_queries"))
    failed = number(raw.get("errored_queries"))
    _put(out, "success_rate", raw.get("uptime"))
    _put(out, "error_rate", _ratio(failed, total))
    _put(out, "requests_total", total)
    _put(out, "requests_failed", failed)
    _put(out, "requests_unfinished", raw.get("incomplete_queries"))
    return frozenset(
        {
            *(f"{m}_ms@{s}" for m in ("ttft", "itl") for s in ("mean", "p50", "p99")),
            "success_rate",
            "error_rate",
            "requests_total",
            "requests_failed",
            "requests_unfinished",
        }
    )


OPENCOMPASS_TASKS = (
    "aime2025",
    "gpqa_diamond",
    "ifeval",
    "mmlu_pro",
    "hle",
    "livecodebench_v6",
    "simpleqa",
    "longbench_v2",
)


def _opencompass(out: dict[str, float], raw: dict[str, Any]) -> frozenset[str]:
    supports = {"ttft_ms@mean", "tpot_ms@mean", "request_output_tps@mean", "output_tpm"}
    for key, value in raw.items():
        # Measures are shares. The module reports a task's score as a share
        # (0.83) and, in older releases, as a percentage (83.0): above 1 can
        # only be a percentage. Read the other way, a share became 0.0083 and a
        # quality floor relative to it passed everything.
        if key in OPENCOMPASS_TASKS or (
            key not in _OPENCOMPASS_PERF and number(value) is not None and "." not in key
        ):
            n = number(value)
            if n is not None:
                out[f"accuracy[{key}]"] = n / 100.0 if n > 1 else n
                supports.add(f"accuracy[{key}]")
    for task in OPENCOMPASS_TASKS:
        supports.add(f"accuracy[{task}]")
    _put(out, "ttft_ms@mean", raw.get("ttft_mean_ms"))
    _put(out, "tpot_ms@mean", raw.get("tpot_mean_ms"))
    # Its `output_tps` is per request; `service_tps` is the aggregate.
    _put(out, "request_output_tps@mean", raw.get("output_tps"))
    service = number(raw.get("service_tps"))
    if service is not None:
        out["output_tpm"] = service * 60
    return frozenset(supports)


_OPENCOMPASS_PERF = {"ttft_mean_ms", "tpot_mean_ms", "output_tps", "service_tps", "gen_tokens"}


def _functional(out: dict[str, float], raw: dict[str, Any]) -> frozenset[str]:
    _put(out, "pass_rate", raw.get("pass_rate"))
    return frozenset({"pass_rate"})


def _tool_call(out: dict[str, float], raw: dict[str, Any]) -> frozenset[str]:
    _put(out, "tool_call_success_rate", raw.get("tool_call_success_rate"))
    _put(out, "error_rate", raw.get("error_rate"))
    _put(out, "requests_total", raw.get("total_requests"))
    _put(out, "requests_completed", raw.get("completed_requests"))
    _put(out, "requests_failed", raw.get("error_requests"))
    _put(out, "requests_truncated", raw.get("truncated_requests"))
    return frozenset(
        {
            "tool_call_success_rate",
            "error_rate",
            "requests_total",
            "requests_completed",
            "requests_failed",
            "requests_truncated",
        }
    )


def _hallucination(out: dict[str, float], raw: dict[str, Any]) -> frozenset[str]:
    _put(out, "hallucination_rate", raw.get("hallucination_rate"))
    _put(out, "requests_total", raw.get("total_requests"))
    _put(out, "requests_completed", raw.get("completed_requests"))
    _put(out, "requests_failed", raw.get("error_requests"))
    return frozenset(
        {"hallucination_rate", "requests_total", "requests_completed", "requests_failed"}
    )


def _case_truncation(out: dict[str, float], raw: dict[str, Any]) -> frozenset[str]:
    _put(out, "long_output_completion_rate", raw.get("completion_ratio"))
    _put(out, "truncated_rate", raw.get("truncation_rate"))
    _put(out, "error_rate", raw.get("error_rate"))
    _put(out, "requests_total", raw.get("total_requests"))
    return frozenset(
        {"long_output_completion_rate", "truncated_rate", "error_rate", "requests_total"}
    )


_QUALITY = {
    "agentic": _agentic,
    "opencompass": _opencompass,
    "functional_acceptance": _functional,
    "tool_call_success": _tool_call,
    "hallucination": _hallucination,
    "case_truncation": _case_truncation,
}


# -- recognizing a module -----------------------------------------------------------


FAMILIES = ("perf_guidellm_sweep", "perf_guidellm", "replay", *_QUALITY)


def family_of(
    module: str, replay_names: tuple[str, ...] = (), aliases: dict[str, str] | None = None
) -> str:
    """The adapter a module name gets: its own, or the family a deployment
    mapped it onto."""
    if module in ("replay", *replay_names):
        return "replay"
    mapped = (aliases or {}).get(module, module)
    return mapped if mapped in FAMILIES else "unknown"


_KIND = {
    "perf_guidellm_sweep": "sweep",
    "perf_guidellm": "single",
    "replay": "replay",
}


def cards_from_card_norm(raw: dict[str, Any]) -> float | None:
    """The cards LLMBench was told the endpoint ran on, recovered from a rate
    it reported twice (`x` and `x_card_norm` = x × 8 ÷ cards). For a result
    whose card count was not recorded anywhere else."""
    for base_key, scale in (
        ("output_tpm", 1.0),
        ("output_tps", 60.0),
        ("input_tpm", 1.0),
        ("total_tps_mean", 60.0),
    ):
        norm_key = (base_key.replace("_tps_mean", "_tpm").replace("_tps", "_tpm")) + "_card_norm"
        base, norm = number(raw.get(base_key)), number(raw.get(norm_key))
        if base and norm:
            cards = CARD_NORM_BASELINE * base * scale / norm
            return round(cards) if abs(cards - round(cards)) < 0.05 else cards
    return None


def adapt(
    key: str,
    raw: dict[str, Any],
    params: dict[str, Any] | None = None,
    cards: float | None = None,
    replay_names: tuple[str, ...] = (),
    aliases: dict[str, str] | None = None,
) -> Scenario:
    """One module run's raw metrics (keys without the module prefix, a sweep's
    levels as `c8.output_tps_mean`) as a Scenario."""
    module = key.split("#", 1)[0]
    family = family_of(module, replay_names, aliases)
    params = params or {}
    sc = Scenario(key=key, module=module, family=family, kind=_KIND.get(family, "quality"))
    cards = cards or cards_from_card_norm(raw)
    if family == "perf_guidellm_sweep":
        _sweep(sc, raw, params, cards)
    elif family == "perf_guidellm":
        _single(sc, raw, params, cards)
    elif family == "replay":
        _replay(sc, raw, params, cards)
    else:
        out: dict[str, float] = {}
        sc.supports = _QUALITY[family](out, raw) if family in _QUALITY else frozenset()
        sc.points["operating"] = out
        sc.operating_rule = "the module's whole run"
    # Every module has the benchmark's own score for it.
    score = number(raw.get("score"))
    if score is not None:
        sc.points.setdefault("operating", {})["score"] = score
        if family == "replay" and cards:
            sc.points["operating"]["score_per_gpu"] = score / cards
    sc.supports = sc.supports | {"score"} | ({"score_per_gpu"} if family == "replay" else set())
    return sc
