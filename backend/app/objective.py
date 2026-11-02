"""Objective evaluation: a target metric plus redlines.

The objective was never "maximize throughput" — it was "maximize throughput
*subject to* latency SLOs". Without the second half, a config that wins on raw
throughput while blowing past an acceptable TTFT looks like the answer, and the
tp=2 vs tp=4 result on node-24 shows the two orderings can disagree completely.

A *redline* is a limit a config must respect to count at all: crossing one
rejects the run however fast it was. Named that way, and only that way,
throughout the platform — "constraint", "limit" and "goal" were three words for
two ideas, and the report, the UI and the API each picked a different one.

Objective shape (stored on the campaign, all fields optional):

    {
      "target_metric": "perf_guidellm_sweep.output_tpm_card_norm",
      "direction": "maximize",
      "redlines": [
        {"metric": "perf_guidellm_sweep.ttft_p99_ms", "op": "<=", "value": 5000},
        {"metric": "functional_acceptance.pass_rate", "op": ">=", "value": 0.99}
      ]
    }

A redline may be relative to production: `"relative_to": "production"` makes
`value` an offset from production's own score on the metric (the average of
its measurements in the campaign), e.g. {"metric": "opencompass/accuracy[gpqa_diamond]",
"op": ">=", "relative_to": "production", "value": -0.10}: no more than ten
points below production. app.quality resolves it before a result is judged;
unresolved (production not measured yet) it is crossed.
"""

from dataclasses import dataclass
from typing import Any

from app.metrics_catalog import DEFAULT_TARGET_METRIC
from app.metrics_catalog import default_direction as _catalog_direction

OPERATORS = {
    "<=": lambda a, b: a <= b,
    "<": lambda a, b: a < b,
    ">=": lambda a, b: a >= b,
    ">": lambda a, b: a > b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}

DEFAULT_TARGET = DEFAULT_TARGET_METRIC


@dataclass
class RedlineResult:
    metric: str
    op: str
    limit: Any
    actual: Any
    ok: bool
    # Why it could not be judged, when it could not.
    note: str = ""
    # Where a relative limit came from ("production 0.71 - 0.10").
    basis: str = ""

    def describe(self) -> str:
        if self.note:
            return f"{self.metric}: {self.note}"
        if self.basis and isinstance(self.actual, int | float):
            return f"{self.metric}={self.actual:g} {self.op} {self.limit:g} ({self.basis})"
        if self.actual is None:
            return f"{self.metric} missing (needed {self.op} {self.limit})"
        return f"{self.metric}={self.actual:g} {self.op} {self.limit:g}" if isinstance(
            self.actual, int | float
        ) else f"{self.metric}={self.actual} {self.op} {self.limit}"


def target_metric(objective: dict | None) -> str:
    return (objective or {}).get("target_metric") or DEFAULT_TARGET


def redlines(objective: dict | None) -> list[dict]:
    """The declared redlines. Campaigns created before the rename carry them
    under "constraints"; a night that already ran must still rank the way it
    was configured, so the old key is read, never rewritten."""
    declared = objective or {}
    return declared.get("redlines") or declared.get("constraints") or []


def direction(objective: dict | None) -> str:
    """"maximize" or "minimize". Falls back to what the metric implies, so
    picking a latency metric does not silently rank the slowest config first."""
    declared = (objective or {}).get("direction")
    if declared in ("maximize", "minimize"):
        return declared
    return default_direction(target_metric(objective))


def default_direction(name: str) -> str:
    """Which way is better for a target: a measure says so in the catalogue,
    a raw key in the metrics catalog (maximize when neither knows)."""
    from app.measures import is_measure_ref, lookup, parse

    if is_measure_ref(name):
        try:
            spec = lookup(parse(name).measure)
        except ValueError:
            spec = None
        return "minimize" if spec is not None and spec.better == "lower" else "maximize"
    return _catalog_direction(name)


def sort_key(value: float | None, objective: dict | None) -> tuple[bool, float]:
    """Ranking key — first element pushes missing values to the bottom."""
    if value is None:
        return (True, 0.0)
    return (False, value if direction(objective) == "minimize" else -value)


def improvement_pct(
    value: float | None, baseline: float | None, objective: dict | None
) -> float | None:
    """How much better than the baseline, as a positive percentage.

    Direction-aware: for a latency objective an improvement is a DECREASE, and
    reporting that as "-27%" would read as a regression.
    """
    if value is None or not baseline:
        return None
    raw = (value - baseline) / abs(baseline) * 100
    return -raw if direction(objective) == "minimize" else raw


def evaluate_redlines(
    objective: dict | None, metrics: dict[str, Any] | None
) -> list[RedlineResult]:
    """Check every declared redline against a run's metrics.

    A missing metric CROSSES its redline: we cannot certify an SLO we did not
    measure, and silently passing would promote exactly the configs whose
    benchmark did not report the number we care about.
    """
    results: list[RedlineResult] = []
    metrics = metrics or {}
    for redline in redlines(objective):
        metric = redline.get("metric")
        op = redline.get("op", "<=")
        limit = redline.get("value")
        if redline.get("relative_to"):
            # app.quality.resolve writes the absolute limit in `value` and
            # drops `relative_to`; one still here had nothing to resolve from.
            actual = metrics.get(metric) if metric and metrics else None
            results.append(RedlineResult(
                str(metric), str(op), None, actual, ok=False,
                note=f"no {redline['relative_to']} score to compare with yet",
            ))
            continue
        # A plain metrics dict, or a measures Reader that also answers measure
        # references (app.measures.Reader) — both have `.get`.
        actual = metrics.get(metric) if metric else None
        compare = OPERATORS.get(op)
        if compare is None or metric is None or limit is None:
            results.append(RedlineResult(str(metric), str(op), limit, actual, ok=False))
            continue
        ok = False
        if actual is not None:
            try:
                ok = bool(compare(actual, limit))
            except TypeError:
                ok = False
        results.append(RedlineResult(metric, op, limit, actual, ok,
                                     basis=str(redline.get("basis") or "")))
    return results


def holds_redlines(objective: dict | None, metrics: dict[str, Any] | None) -> bool:
    return all(result.ok for result in evaluate_redlines(objective, metrics))


def breaches(objective: dict | None, metrics: dict[str, Any] | None) -> list[str]:
    """Human-readable descriptions of the redlines this run crossed."""
    return [r.describe() for r in evaluate_redlines(objective, metrics) if not r.ok]
