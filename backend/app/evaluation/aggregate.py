"""Raw benchmark metrics reduced against one objective — computed once, when
the result lands.

The verdict "this run scored X and held every redline" was previously derived
at read time, independently, by the report and by the leaderboard. Two
consequences: a search could not see it at all (it only ever got the raw
metric dict), and editing a campaign's objective silently re-ranked history
with no record that the numbers had moved.

Computing it at write time fixes both and gives top-K confirmation a stable
notion of "the top" to repeat.

Constraint convention (the one optimizers expect): a constraint is SATISFIED
when its value is <= 0. Each redline becomes one signed slack, oriented so
that crossing it is positive:

    ttft_p99 <= 5000   ->   ttft_p99 - 5000
    pass_rate >= 0.99  ->   0.99 - pass_rate

A missing metric cannot be certified, so it crosses (see app.objective) and is
reported as +inf slack rather than as a satisfied constraint.
"""

import math
from dataclasses import dataclass, field
from typing import Any

from app.objective import direction, evaluate_redlines, target_metric

# Redlines are "stay inside this"; the slack is oriented so > 0 means crossed.
_SLACK_SIGN: dict[str, float] = {"<=": 1.0, "<": 1.0, ">=": -1.0, ">": -1.0}


@dataclass(frozen=True)
class ResultSummary:
    """One benchmark result, reduced against one objective."""

    objective_value: float | None
    feasible: bool
    constraints: tuple[float, ...] = ()  # satisfied when <= 0
    breaches: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """The persisted form.

        `constraints` becomes a list (JSON has no tuples) with every
        non-finite slack written as null: Postgres JSONB rejects `Infinity`
        outright, and Python's json emits it happily, so storing the raw tuple
        would pass on sqlite in tests and fail on the real database at 3am.
        Null round-trips back to +inf via `constraints_from_stored`.
        """
        return {
            "objective_value": self.objective_value,
            "feasible": self.feasible,
            "constraints": [None if not math.isfinite(c) else c for c in self.constraints],
            "breaches": list(self.breaches),
        }


def constraints_from_stored(stored: list[Any] | None) -> tuple[float, ...]:
    """Rebuild the slack tuple persisted by `ResultSummary.as_dict`.

    Null means "crossed, with no measurable distance" — an unmeasured metric
    or an operator that has no ordering. +inf is the honest reading: no finite
    step in any direction makes it satisfied.
    """
    return tuple(math.inf if c is None else float(c) for c in (stored or []))


def _numeric(value: Any) -> float | None:
    """Metrics arrive from an external platform; a string or a null where a
    number was expected must not raise in the middle of a night."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _slack(op: str, actual: Any, limit: Any) -> float:
    """Signed distance from a redline: <= 0 satisfied, > 0 crossed.

    An unmeasured or non-numeric metric is maximally infeasible rather than
    quietly satisfied — the same rule app.objective applies to the boolean.
    """
    sign = _SLACK_SIGN.get(op)
    a, b = _numeric(actual), _numeric(limit)
    if sign is None or a is None or b is None:
        # `==`/`!=` have no meaningful distance, and a missing value has no
        # distance at all. Both are only expressible as "crossed".
        return math.inf
    return sign * (a - b)


def summarize(
    objective: dict[str, Any] | None, metrics: dict[str, Any] | None
) -> ResultSummary:
    """Reduce a run's metrics to (value, feasibility) under `objective`.

    Ranking semantics are deliberately NOT applied here: `objective_value` is
    the raw target metric, and `direction` decides what "better" means at
    comparison time (app.objective.sort_key). Negating minimize-objectives
    here would make the stored number disagree with the one in the report.
    """
    metrics = metrics or {}
    value = _numeric(metrics.get(target_metric(objective)))

    results = evaluate_redlines(objective, metrics)
    constraints = tuple(_slack(r.op, r.actual, r.limit) for r in results)
    crossed = [r.describe() for r in results if not r.ok]

    # A run with no measured target metric is not a usable observation however
    # clean its redlines were: there is nothing to rank it by.
    feasible = not crossed and value is not None
    return ResultSummary(
        objective_value=value,
        feasible=feasible,
        constraints=constraints,
        breaches=crossed,
    )


def summary_of(result: Any, objective: dict[str, Any] | None) -> ResultSummary:
    """The summary stored on a result row, or one computed from its metrics.

    Rows written before migration 010 carry no summary, and so do the plain
    objects the report tests build — both fall back to computing, which gives
    the identical answer for the same (objective, metrics). The stored value
    only *differs* when a campaign's objective was edited after the run, and
    there it is the honest one: it is what the run was actually judged by.
    """
    if result is None:
        return ResultSummary(objective_value=None, feasible=False)
    stored_value = getattr(result, "objective_value", None)
    stored_breaches = getattr(result, "breaches", None)
    if stored_value is not None or stored_breaches:
        return ResultSummary(
            objective_value=stored_value,
            feasible=bool(getattr(result, "feasible", False)),
            constraints=constraints_from_stored(getattr(result, "constraints", None)),
            breaches=list(stored_breaches or []),
        )
    return summarize(objective, getattr(result, "metrics", None))


def is_better(a: float | None, b: float | None, objective: dict | None) -> bool:
    """Is `a` a better objective value than `b`? Direction-aware."""
    if a is None:
        return False
    if b is None:
        return True
    return a < b if direction(objective) == "minimize" else a > b
