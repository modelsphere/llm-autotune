"""Reducing metrics to (value, feasibility) is the one place the platform
decides what a run was worth. The report, the leaderboard, top-K confirmation
and every planner read the same answer from here, so a disagreement would show
up as the UI and the searcher crowning different configs."""

import json
import math
from types import SimpleNamespace

from app.evaluation.aggregate import (
    ResultSummary,
    constraints_from_stored,
    is_better,
    summarize,
    summary_of,
)

SLO = {
    "target_metric": "tpm_card",
    "redlines": [
        {"metric": "ttft_p99_ms", "op": "<=", "value": 7000},
        {"metric": "pass_rate", "op": ">=", "value": 0.99},
    ],
}
FAST_BUT_LATE = {"tpm_card": 68425.0, "ttft_p99_ms": 8692.0, "pass_rate": 1.0}
WITHIN_SLO = {"tpm_card": 43607.0, "ttft_p99_ms": 6324.0, "pass_rate": 1.0}


def test_a_config_inside_every_redline_is_feasible():
    summary = summarize(SLO, WITHIN_SLO)
    assert summary.feasible
    assert summary.objective_value == 43607.0
    assert not summary.breaches
    assert all(c <= 0 for c in summary.constraints), "satisfied means <= 0"


def test_constraint_slack_is_signed_so_crossing_is_positive():
    summary = summarize(SLO, FAST_BUT_LATE)
    assert not summary.feasible
    # ttft 8692 against a 7000 limit is 1692 over; pass_rate 1.0 against a
    # 0.99 floor is 0.01 to spare, which reads as -0.01.
    assert summary.constraints[0] == 8692.0 - 7000
    assert summary.constraints[1] < 0


def test_a_greater_than_redline_is_oriented_the_same_way():
    """`>=` constraints have to be negated, or a comfortably-passing accuracy
    check would look like the worst violation in the set."""
    summary = summarize(SLO, {**WITHIN_SLO, "pass_rate": 0.5})
    assert summary.constraints[1] == 0.99 - 0.5 > 0
    assert not summary.feasible


def test_an_unmeasured_metric_is_infeasible_not_satisfied():
    """We cannot certify an SLO we did not measure; a silent pass promotes
    exactly the configs whose benchmark omitted the number."""
    summary = summarize(SLO, {"tpm_card": 1.0, "pass_rate": 1.0})
    assert not summary.feasible
    assert math.isinf(summary.constraints[0])
    assert "missing" in summary.breaches[0]


def test_a_run_without_the_target_metric_is_not_feasible():
    """Clean redlines are not enough: with nothing to rank it by, the point
    cannot be compared against anything."""
    summary = summarize({"target_metric": "absent"}, {"other": 1.0})
    assert summary.objective_value is None
    assert not summary.feasible


def test_no_redlines_means_unconstrained_not_all_crossed():
    summary = summarize({"target_metric": "tpm_card"}, FAST_BUT_LATE)
    assert summary.feasible
    assert summary.constraints == ()


def test_the_persisted_form_survives_json():
    """Postgres JSONB rejects Infinity outright, and Python's json emits it
    happily — storing the raw tuple would pass on sqlite and fail on the real
    database in the middle of a night."""
    stored = summarize(SLO, {"tpm_card": 1.0}).as_dict()
    round_tripped = json.loads(json.dumps(stored))  # would raise on inf
    assert None in round_tripped["constraints"]
    assert math.isinf(constraints_from_stored(round_tripped["constraints"])[0])


def test_a_stored_summary_wins_over_recomputing():
    """The objective can be edited after a night ran. The stored verdict is
    what the run was actually judged by; recomputing would rewrite history."""
    row = SimpleNamespace(
        objective_value=99.0,
        feasible=True,
        constraints=[-1.0],
        breaches=[],
        metrics=FAST_BUT_LATE,  # would breach if recomputed under SLO
    )
    assert summary_of(row, SLO) == ResultSummary(99.0, True, (-1.0,), [])


def test_a_row_without_a_summary_falls_back_to_computing():
    """Results written before the objective was resolved at write time, and
    the plain objects the report tests build."""
    row = SimpleNamespace(metrics=WITHIN_SLO)
    assert summary_of(row, SLO).objective_value == 43607.0
    assert summary_of(None, SLO).objective_value is None


def test_better_is_direction_aware():
    faster = {"target_metric": "ttft_p99_ms", "direction": "minimize"}
    assert is_better(6324.0, 8692.0, faster)
    assert not is_better(8692.0, 6324.0, faster)
    assert is_better(68425.0, 43607.0, {"target_metric": "tpm_card"})
    assert is_better(1.0, None, None), "any measurement beats none"


def test_nested_metric_groups_are_flattened_into_addressable_keys():
    """Shape taken from campaign 20: guidellm returns one group per concurrency
    level, each a full metric set. Left nested, the single-stream numbers were
    in the database and impossible to write an objective against."""
    from app.evaluation.llmbench import _flatten_metrics

    submission = {
        "score_total": 1.62,
        "runs": [
            {
                "module_name": "perf_guidellm_sweep",
                "score": 1.62,
                "metrics_json": {
                    "output_tps": 257.79,
                    "c1": {"request_output_tps": 129.82, "ttft_p99_ms": 3268.8},
                    "c4": {"request_output_tps": 64.45},
                },
            }
        ],
    }
    metrics = _flatten_metrics(submission)

    assert metrics["perf_guidellm_sweep.output_tps"] == 257.79
    assert metrics["perf_guidellm_sweep.c1.request_output_tps"] == 129.82
    assert metrics["perf_guidellm_sweep.c1.ttft_p99_ms"] == 3268.8
    assert metrics["perf_guidellm_sweep.c4.request_output_tps"] == 64.45
    assert metrics["perf_guidellm_sweep.score"] == 1.62
    assert metrics["score_total"] == 1.62
    # The group itself is not left behind as a dict-valued "metric".
    assert "perf_guidellm_sweep.c1" not in metrics
    assert all(not isinstance(v, dict | list) for v in metrics.values())


def test_flattening_is_generic_rather_than_keyed_to_known_module_names():
    """LLMBench is external and its tests change. A new module with different
    field names has to land as data, not as a code change."""
    from app.evaluation.llmbench import _flatten_metrics

    metrics = _flatten_metrics(
        {
            "runs": [
                {
                    "module_name": "some_future_suite",
                    "metrics_json": {"whatever": 1.0, "grouped": {"nested": 2.0}},
                }
            ]
        }
    )
    assert metrics == {
        "some_future_suite.whatever": 1.0,
        "some_future_suite.grouped.nested": 2.0,
    }


def test_flattening_is_depth_bounded_and_drops_series():
    """A deeply nested or list-valued field is not something a redline can
    point at, and must not explode into thousands of keys. `results.raw`
    keeps the original either way."""
    from app.evaluation.llmbench import _flatten_metrics

    metrics = _flatten_metrics(
        {
            "runs": [
                {
                    "module_name": "m",
                    "metrics_json": {
                        "deep": {"a": {"b": {"c": 1.0}}},
                        "series": [1, 2, 3],
                        "ok": 5.0,
                    },
                }
            ]
        }
    )
    assert metrics == {"m.ok": 5.0}


def test_a_non_numeric_metric_does_not_raise():
    """Metrics come from an external platform; a string where a number was
    expected must not take down the tick loop."""
    summary = summarize(SLO, {"tpm_card": "n/a", "ttft_p99_ms": None, "pass_rate": 1.0})
    assert summary.objective_value is None
    assert not summary.feasible
