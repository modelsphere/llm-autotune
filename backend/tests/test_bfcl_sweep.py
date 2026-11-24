"""BFCL concurrency plans, frozen protocol identities and report selection."""

import pytest

from app.agent.parts import best_level_of
from app.evaluation.benchmark_spec import BenchmarkSpec, BFCLSpec, document
from app.evaluation.bfcl_sweep import BFCLSweepPlan, bfcl_levels, select_bfcl_level
from app.evaluation.llmbench import _flatten_metrics
from app.evaluation.scenarios import scenarios_of
from app.schemas.agent import ScenarioOut


@pytest.mark.parametrize("minimum,maximum,expected", [
    (1, 8, [1, 3, 5, 6, 8]), (4, 18, [4, 8, 11, 15, 18]),
])
def test_linear_integer_points_include_endpoints_and_round_half_up(minimum, maximum, expected):
    plan = BFCLSweepPlan(min=minimum, max=maximum, points=5)
    assert plan.resolved() == expected
    assert plan.snapshot()["sampling_version"] == 1
    spec = BenchmarkSpec(kind="bfcl", bfcl={"concurrency_grid": plan.resolved()})
    assert document(spec)["modules"][0]["params"]["concurrency_grid"] == expected
    assert spec.slug != BenchmarkSpec(kind="bfcl").slug


@pytest.mark.parametrize("plan", [
    {"min": 1, "max": 3, "points": 5}, {"min": 8, "max": 4},
    {"min": 0, "max": 8}, {"min": 4, "max": 129}, {"min": 1.0, "max": 8},
])
def test_invalid_plans_are_refused(plan):
    with pytest.raises(ValueError):
        BFCLSweepPlan(**plan)


def sample():
    raw = {"sweep_complete": 1, "reported_concurrency": 4, "total_tpm": 4000,
           "c1": {"total_tpm": 1000, "accuracy": 0.95, "valid": 1, "eligible": 1},
           "c4": {"total_tpm": 4000, "accuracy": 0.85, "valid": 1, "eligible": 1},
           "c8": {"total_tpm": 8000, "accuracy": 0.5, "valid": 1, "eligible": 0}}
    metrics = _flatten_metrics({"runs": [{"module_name": "bfcl_v4", "metrics_json": raw}]})
    report = {"module": "bfcl_v4", "params": {"concurrency": 2, "concurrency_grid": [1, 4, 8]},
              "bounds": [{"metric": "accuracy", "role": "redline", "min": 0.8}]}
    return raw, metrics, report


def test_selection_respects_all_floors_and_completion_and_keeps_every_level():
    raw, metrics, report = sample()
    assert len(bfcl_levels(metrics, "bfcl_v4")) == 3
    assert select_bfcl_level(metrics, report)["concurrency"] == 4
    assert select_bfcl_level(metrics, report, {"bfcl_v4.accuracy": 0.9})["concurrency"] == 1
    summary = scenarios_of(metrics, [report], "p90")[0]
    assert summary["case_concurrency"] == 4
    scenario = ScenarioOut(key="bfcl_v4", kind="bfcl", summary=summary,
                          metrics=raw, levels=bfcl_levels(metrics, "bfcl_v4"))
    best = best_level_of(scenario, cards=2, ttft_percentile="p90", gate=None)
    assert best["case_concurrency"] == 4
    metrics["bfcl_v4.sweep_complete"] = 0
    assert select_bfcl_level(metrics, report)["concurrency"] == 4
    scenario.metrics["sweep_complete"] = 0
    best = best_level_of(scenario, cards=2, ttft_percentile="p90", gate=None)
    assert best["case_concurrency"] == 4
    metrics["bfcl_v4.sweep_canceled"] = 1
    scenario.metrics["sweep_canceled"] = 1
    assert select_bfcl_level(metrics, report) is None
    assert best_level_of(scenario, cards=2, ttft_percentile="p90", gate=None) == {}


def test_grid_validation_and_fixed_identity_remain_compatible():
    assert "concurrency_grid" not in document(BenchmarkSpec(kind="bfcl"))["modules"][0]["params"]
    for grid in ([1, 1], [8, 1], [0, 4], [1, 129]):
        with pytest.raises(ValueError):
            BFCLSpec(concurrency_grid=grid)


def test_frozen_module_report_retains_the_effective_grid():
    from app.evaluation.llmbench import module_reports

    report = module_reports({"runs": [{"module_name": "bfcl_v4", "params_json": {
        "concurrency": 2, "concurrency_grid": [1, 3, 5, 6, 8],
        "required_metrics": ["accuracy", "total_tpm"], "unrecognized_nested": {"x": 1},
    }}]})[0]
    assert report["params"]["concurrency_grid"] == [1, 3, 5, 6, 8]
    assert "unrecognized_nested" not in report["params"]


@pytest.mark.parametrize("status,error", [
    ("canceled", ""),
    ("failed", "Canceled"),
    ("done", "BFCL scan canceled"),
    ("failed", ""),
    ("done", "Could not persist BFCL artifacts"),
    ("done", "BFCL required benchmark metric unavailable: ttft_p90_ms"),
])
def test_canceled_or_failed_results_cannot_be_reselected(status, error):
    _, metrics, report = sample()
    report.update(status=status, error=error)
    assert select_bfcl_level(metrics, report) is None


def test_scan_level_redlines_are_evaluated_at_scan_level():
    _, metrics, report = sample()
    report["bounds"].append({"metric": "sweep_complete", "role": "redline", "min": 1})
    assert select_bfcl_level(metrics, report)["concurrency"] == 4
    assert select_bfcl_level(metrics, report, {"bfcl_v4.accuracy": 0.9})["concurrency"] == 1
    metrics["bfcl_v4.sweep_complete"] = 0
    assert select_bfcl_level(metrics, report) is None

    metrics["bfcl_v4.sweep_complete"] = 1
    report["bounds"].append({"metric": "reported_concurrency", "role": "redline", "min": 4})
    assert select_bfcl_level(metrics, report)["concurrency"] == 4
    assert select_bfcl_level(metrics, report, {"bfcl_v4.accuracy": 0.9}) is None


def test_results_part_keeps_best_level_under_scan_level_redlines():
    from types import SimpleNamespace

    from app.agent.parts import results_part

    raw, metrics, report = sample()
    run_entry = {
        "module_name": "bfcl_v4", "status": "done", "passed": True,
        "params_json": report["params"], "metrics_json": raw,
        "metric_configs_json": [
            {"key": "accuracy", "role": "redline", "min_val": 0.8},
            {"key": "sweep_complete", "role": "redline", "min_val": 1},
        ],
    }
    bundle = SimpleNamespace(
        run=SimpleNamespace(id=1, status="done", failure_class="", error="", env_snapshot={}),
        result=SimpleNamespace(
            metrics=metrics,
            raw={"runs": [run_entry]},
            passed=True, feasible=True, score=0.85, objective_value=4000.0, constraints=[],
        ),
        campaign=None,
        candidate=None,
        machine=None,
        overlay=None,
    )
    part = results_part(bundle)
    scenario = part.scenarios[0]
    assert [lv.metrics["eligible"] for lv in scenario.levels] == [1, 1, 0]
    assert scenario.best_level["case_concurrency"] == 4

    run_entry["status"] = "failed"
    run_entry["passed"] = False
    run_entry["error"] = "Could not persist BFCL artifacts"
    failed_part = results_part(bundle)
    assert [lv.metrics["eligible"] for lv in failed_part.scenarios[0].levels] == [0, 0, 0]
    assert failed_part.scenarios[0].best_level == {}


def test_scan_budget_matches_47h_worker_limit_and_title_shows_grid():
    assert BFCLSpec(concurrency_grid=[1, 4], max_seconds=84600).max_seconds == 84600
    assert BFCLSpec(concurrency_grid=[1, 4, 8], max_seconds=86400,
                    max_total_seconds=47 * 3600).max_total_seconds == 47 * 3600
    with pytest.raises(ValueError, match="47 hours"):
        BFCLSpec(concurrency_grid=[1, 4, 8], max_seconds=86400)
    with pytest.raises(ValueError):
        BFCLSpec(concurrency_grid=[1, 4], max_total_seconds=47 * 3600 + 1)
    spec = BenchmarkSpec(kind="bfcl", bfcl={"concurrency_grid": [1, 4, 8]})
    assert "case concurrency grid 1,4,8" in spec.title
