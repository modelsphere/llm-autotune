"""Reading a benchmark's module reports as scenarios.

A benchmark runs several modules. Some of them measure throughput under a
described load — a synthetic sweep at a given input/output token shape, or a
replay of recorded traffic — and those are what a report compares configs on.
Others (a quality suite, say) are not scenarios at all and are read for their
verdict instead.

Which is which is decided SHAPE-FIRST, from the module's own params rather
than from a list of known module names: a benchmark that grows a fourth
scenario needs no change here. A sweep declares input and output token
counts; a replay declares neither, because it plays back real requests at one
fixed concurrency, and is recognized by its module name instead.
"""

from __future__ import annotations

from typing import Any

# The metric a benchmark reports to say whether the level it settled on met
# the SLO it was given.
GATE_FLAG = "reported_level_meets_slo"


def tokens_label(n: Any) -> str:
    """`50000` as `50k`, for a scenario's name."""
    try:
        value = float(n)
    except (TypeError, ValueError):
        return "?"
    if value >= 1000:
        short = value / 1000
        return f"{short:.0f}k" if short == int(short) else f"{short:.1f}k"
    return f"{value:.0f}"


def as_flag(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    text = str(value).strip().lower()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no"):
        return False
    return None


def as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def is_sweep(report: dict) -> bool:
    params = report.get("params") or {}
    return "input_tokens" in params and "output_tokens" in params


def is_replay(report: dict) -> bool:
    """A traffic-replay module: recognized by name, since its params share
    nothing with a sweep's. Any module whose name starts with `replay`."""
    name = str(report.get("module_name") or report.get("module") or "")
    return name.split("#")[0].startswith("replay")


def is_scenario(report: dict) -> bool:
    return is_sweep(report) or is_replay(report)


def scenario_label(report: dict) -> str:
    """How a scenario is named in a report: `50k + 1.5k` for a sweep,
    `replay` for a traffic replay, "" for a module that is not a scenario."""
    params = report.get("params") or {}
    if is_sweep(report):
        return f"{tokens_label(params['input_tokens'])} + {tokens_label(params['output_tokens'])}"
    if is_replay(report):
        return "replay"
    return ""


def scenarios_of(metrics: dict[str, Any], module_reports: list, ttft_percentile: str) -> list[dict]:
    """One entry per module run that declares a scenario, with the numbers a
    report shows: per-card-normalized total TPM at the reported level, the
    level itself, per-request output tok/s, TTFT at the given percentile, and
    whether the reported level met the SLO.

    A replay has no reported level and no gate flag: its concurrency is the
    one it was run at, its per-request rate the mean over requests, and
    `meets_gate` stays None — its own redlines judge it, through `passed`.
    """
    out = []
    for report in module_reports or []:
        if not isinstance(report, dict):
            continue
        if not is_scenario(report):
            continue
        params = report.get("params") or {}
        key = str(report.get("module") or report.get("module_name") or "")
        replay = is_replay(report)
        total = as_number(metrics.get(f"{key}.total_tpm_card_norm"))
        output = as_number(metrics.get(f"{key}.output_tpm_card_norm"))
        concurrency = as_number(metrics.get(f"{key}.reported_concurrency"))
        request_tps = as_number(metrics.get(f"{key}.request_output_tps"))
        if replay:
            concurrency = as_number(params.get("concurrency"))
            request_tps = as_number(metrics.get(f"{key}.output_tps_mean"))
        out.append({
            "key": key,
            "kind": "replay" if replay else "sweep",
            "label": scenario_label(report),
            "input_tokens": params.get("input_tokens"),
            "output_tokens": params.get("output_tokens"),
            "tpm_card_norm": total,
            "output_tpm_card_norm": output,
            # Input is what is LEFT of the total, deliberately not the sweep's
            # own `input_tpm_card_norm`: that one is a mean over each request's
            # prefill window, so overlapping requests count the same second
            # more than once. `total` is tokens over wall clock, which is the
            # rate a day of serving is actually made of.
            "input_tpm_card_norm": (
                max(total - output, 0.0) if total is not None and output is not None else None
            ),
            "concurrency": concurrency,
            "request_output_tps": request_tps,
            "ttft_ms": as_number(metrics.get(f"{key}.ttft_{ttft_percentile}_ms")),
            "meets_gate": as_flag(metrics.get(f"{key}.{GATE_FLAG}")),
            "passed": report.get("passed"),
        })
    return out
