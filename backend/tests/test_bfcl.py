"""BFCL specs and measures agree with the native LLMBench module contract."""
import hashlib
import json

import pytest

from app import quality
from app.agent.comparison import deltas_between
from app.agent.parts import best_level_of
from app.evaluation.benchmark_spec import BenchmarkSpec, BFCLSpec, QualitySpec, document
from app.evaluation.llmbench import _flatten_metrics
from app.evaluation.scenarios import is_scenario, scenarios_of
from app.measures import measures_of, problem_with, value
from app.measures.workload import describe
from app.mocks.llmbench_mock import _bfcl_metrics
from app.schemas.agent import ResultsPart, ScenarioOut


def test_bfcl_document_and_protocol_identity():
    spec = BenchmarkSpec(kind="bfcl")
    doc = document(spec)
    assert doc["modules"][0]["module_name"] == spec.module == "bfcl_v4"
    assert doc["group_tags"] == ["llm-autotune/bfcl"]
    params = doc["modules"][0]["params"]
    assert params["cases_per_category"] == 20
    for key, v in {"task_set": "bfcl-v4-multi-turn-long-context-v1", "cases_per_category": 40,
                   "concurrency": 4, "max_seconds": 3600, "request_retries": 1,
                   "min_ttft_coverage": 0.8, "probe_endpoint": True}.items():
        assert BenchmarkSpec(kind="bfcl", bfcl={**params, key: v}).slug != spec.slug
    assert spec.slug == BenchmarkSpec(kind="bfcl", input_tokens=1).slug


def test_bfcl_quality_coexists_with_opencompass_and_preserves_old_slugs():
    q = QualitySpec()
    spec = BenchmarkSpec(kind="replay", quality=q)
    before = {"kind": "replay", "dataset_profile": "", "requests": 500, "concurrency": 16,
              "quality": {**q.model_dump(), "once": True}}
    digest = hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest()[:8]
    assert spec.slug == f"autotune-replay-{digest}"
    checked = BenchmarkSpec(kind="replay", quality=q, bfcl_quality=BFCLSpec())
    modules = document(checked)["modules"]
    assert [m["module_name"] for m in modules] == ["replay", "opencompass", "bfcl_v4"]
    assert [m["order_index"] for m in modules] == [0, 1, 2]
    assert modules[-1]["weight"] == 0
    assert all(m["role"] == "display" for m in modules[-1]["metric_configs"])
    assert spec.slug != checked.slug


@pytest.mark.parametrize("params", [
    {"task_set": "unknown"}, {"cases_per_category": 201}, {"concurrency": 129},
    {"request_retries": 4}, {"min_ttft_coverage": 1.1}, {"max_seconds": 0},
    {"task_set": "bfcl-v4-smoke-v1", "cases_per_category": 2}, {"workers": 16},
])
def test_invalid_protocol_is_refused(params):
    with pytest.raises(ValueError):
        BFCLSpec(**params)


def test_protocol_parameters_cannot_be_ignored_or_duplicated():
    with pytest.raises(ValueError, match="kind=bfcl"):
        BenchmarkSpec(kind="sweep", bfcl=BFCLSpec())
    with pytest.raises(ValueError, match="already measures"):
        BenchmarkSpec(kind="bfcl", bfcl_quality=BFCLSpec())


def test_measures_preserve_missing_data_and_case_http_semantics():
    raw = {"accuracy": 0.8, "accuracy_multi_turn_long_context": 0.75,
           "ttft_p90_ms": 150, "request_e2e_p90_ms": 900, "ttft_p99_ms": None,
           "total_tpm": 12000, "input_tpm": 9000, "output_tpm": 3000,
           "passed_cases_per_hour": 60, "ttft_coverage": 0.9, "http_attempt_count": 20,
           "request_error_count": 2, "retry_count": 2, "active_http_peak": 3}
    sc = measures_of({f"bfcl_v4.{k}": v for k, v in raw.items()},
                     [{"module": "bfcl_v4", "params": {"concurrency": 8}}], cards=2)
    assert value("bfcl_v4/accuracy[overall]", sc) == 0.8
    assert value("bfcl_v4/accuracy[multi_turn_long_context]", sc) == 0.75
    assert value("bfcl_v4/total_tpm_per_gpu", sc) == 6000
    assert value("bfcl_v4/ttft_ms@p90", sc) == 150
    assert value("bfcl_v4/e2e_latency_ms@p90", sc) == 900
    assert value("bfcl_v4/ttft_ms@p99", sc) is None
    assert value("bfcl_v4/success_rate", sc) is None
    assert value("bfcl_v4/concurrency", sc) is None
    assert "concurrency" not in sc[0].supports
    assert problem_with("bfcl_v4/accuracy[overall]", ["bfcl_v4"]) is None
    assert problem_with("bfcl_v4/ttft_ms@mean", ["bfcl_v4"])
    assert problem_with("bfcl_v4/itl_ms@p99", ["bfcl_v4"])
    assert problem_with("bfcl_v4#2/accuracy[overall]", ["bfcl_v4", "bfcl_v4"]) is None


def test_quality_floor_accepts_bfcl_accuracy():
    metric = "bfcl_v4/accuracy[overall]"
    obj = quality.resolve({"redlines": quality.quality_floor([], 0.10, metrics=[metric])},
                          {metric: 0.8})
    assert obj["redlines"][0]["value"] == 0.7
    assert quality.quality_floor(["gpqa_diamond"], 0.1)[0]["metric"] == (
        "opencompass/accuracy[gpqa_diamond]")


def test_reports_and_workload_show_case_semantics_and_generation_rates():
    params = BFCLSpec().model_dump()
    raw = _bfcl_metrics(1, params)
    metrics = _flatten_metrics({"runs": [{"module_name": "bfcl_v4", "metrics_json": raw}]})
    report = {"module": "bfcl_v4", "module_name": "bfcl_v4", "params": params}
    assert is_scenario(report)
    summary = scenarios_of(metrics, [report], "p90")[0]
    assert summary["kind"] == "bfcl" and summary["case_concurrency"] == 2
    assert "concurrency" not in summary
    scenario = ScenarioOut(key="bfcl_v4", kind="bfcl", summary=summary, metrics=raw)
    best = best_level_of(scenario, cards=2, ttft_percentile="p90", gate=None)
    assert best["total_tps_per_gpu"] == raw["total_tpm"] / 120
    assert best["passed_cases_per_hour"] == raw["passed_cases_per_hour"]
    assert "concurrency" not in best
    result = ResultsPart(run_id=1, status="measured", scenarios=[scenario])
    assert "passed_cases_per_hour" in deltas_between(result, result).scenarios[0].metrics
    workload = describe([{"module_name": "bfcl_v4", "params": params}])[0]
    assert workload["params"]["cases_per_category"] == 20
    assert "active cases" in workload["reads_as"]
