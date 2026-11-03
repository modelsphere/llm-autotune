"""Measures: LLMBench's module keys translated into one vocabulary.

The payloads under tests/data/measures are real results; the multi-level
sweeps are built from the real level's shape.
"""

import json
from pathlib import Path

import pytest

from app.measures import MEASURES, RefError, measures_of, parse, value
from app.measures.adapters import adapt, cards_from_card_norm
from app.measures.refs import is_measure_ref

DATA = Path(__file__).parent / "data" / "measures"


def _load(name: str) -> dict:
    return json.loads((DATA / f"{name}.json").read_text())


def _replay():
    d = _load("replay")
    return d["metrics"], measures_of(
        d["metrics"], [{"module": "replay", "params": {"concurrency": 16}}], cards=d["cards"]
    )


# -- references --------------------------------------------------------------------


def test_a_reference_names_measure_stat_qualifier_point_and_scenario():
    ref = parse("replay/ttft_ms@p90[32k_64k]:operating")
    assert (ref.scenario, ref.measure, ref.stat, ref.qualifier, ref.point) == (
        "replay",
        "ttft_ms",
        "p90",
        "32k_64k",
        None,
    )
    assert (
        str(parse("perf_guidellm_sweep#2/request_output_tps:c1"))
        == "perf_guidellm_sweep#2/request_output_tps@mean:c1"
    )


def test_a_raw_llmbench_key_is_never_read_as_a_measure():
    assert not is_measure_ref("replay.output_tpm_card_norm")
    assert not is_measure_ref("perf_guidellm_sweep.c1.request_output_tps")
    assert is_measure_ref("output_tpm_per_gpu")
    # A submission-wide raw key has no dot either.
    assert not is_measure_ref("score_total")
    assert is_measure_ref("ttft_ms@p95")  # a measure, refused when parsed


@pytest.mark.parametrize(
    "text, message",
    [
        ("ttft_ms", "needs a statistic"),
        ("ttft_ms@p95", "no statistic 'p95'"),
        ("output_tpm_per_gpu@p99", "takes no statistic"),
        ("ttft_ms@p90[1k_2k]", "no prompt bucket"),
        ("accuracy", "needs a task"),
        ("throughput", "unknown measure"),
        ("success_rate:c-1", "not a measure reference"),
    ],
)
def test_a_reference_the_catalogue_cannot_answer_is_refused_when_written(text, message):
    with pytest.raises(RefError, match=message):
        parse(text)


def test_uptime_is_accepted_as_the_name_people_already_use():
    assert parse("uptime").measure == "success_rate"


def test_every_measure_with_statistics_lists_only_ones_some_module_reports():
    for m in MEASURES:
        assert set(m.stats) <= {"mean", "p10", "p50", "p90", "p99"}, m.name


# -- replay ------------------------------------------------------------------------


def test_replay_rates_per_gpu_are_one_eighth_of_llmbench_card_norm():
    metrics, scenarios = _replay()
    # LLMBench's card_norm is per EIGHT cards: rate × 8 ÷ cards.
    assert value("output_tpm_per_gpu", scenarios) == pytest.approx(
        metrics["replay.output_tpm_card_norm"] / 8
    )
    assert value("output_tpm_per_gpu", scenarios) == pytest.approx(metrics["replay.output_tpm"] / 2)
    assert value("total_tpm", scenarios) == pytest.approx(
        metrics["replay.input_tpm"] + metrics["replay.output_tpm"]
    )
    assert value("uncached_input_tpm_per_gpu", scenarios) == pytest.approx(
        metrics["replay.uncached_input_tpm"] / 2
    )


def test_replay_per_request_speeds_are_not_mistaken_for_aggregate_rates():
    metrics, scenarios = _replay()
    # A replay's output_tps_* is completion ÷ total time of ONE request.
    assert value("request_output_tps", scenarios) == metrics["replay.output_tps_mean"]
    assert value("request_output_tps@p10", scenarios) == metrics["replay.output_tps_p10"]
    assert value("request_prefill_tps@p50", scenarios) == metrics["replay.input_tps_p50"]


def test_replay_health_counts_and_rates():
    metrics, scenarios = _replay()
    assert value("success_rate", scenarios) == metrics["replay.uptime"]
    assert value("requests_completed", scenarios) == metrics["replay.successful_requests"]
    assert value("truncated_rate", scenarios) == pytest.approx(
        metrics["replay.finish_reason_length_count"] / metrics["replay.successful_requests"]
    )
    assert value("not_started_rate", scenarios) == 0.0
    assert value("request_rate", scenarios) == pytest.approx(
        metrics["replay.successful_requests"] / metrics["replay.wall_time_s"]
    )


def test_replay_ttft_buckets_and_their_sample_sizes():
    metrics, scenarios = _replay()
    assert value("ttft_ms@p90[32k_64k]", scenarios) == metrics["replay.ttft_32k_64k_p90_ms"]
    assert value("ttft_ms@mean[32k_64k]", scenarios) == metrics["replay.ttft_32k_64k_avg_ms"]
    assert (
        value("prompt_bucket_requests[32k_64k]", scenarios) == metrics["replay.ttft_32k_64k_count"]
    )
    # An empty bucket's percentiles are null in LLMBench: absent here, not 0.
    assert value("ttft_ms@p90[ge_256k]", scenarios) is None


def test_replay_has_no_statistic_it_does_not_report():
    _, scenarios = _replay()
    sc = scenarios[0]
    assert "ttft_ms@p99" in sc.supports and "itl_ms@p99" not in sc.supports
    assert value("itl_ms@p99", scenarios) is None


def test_replay_workload_facts_and_judge_left_out_when_off():
    metrics, scenarios = _replay()
    assert value("concurrency", scenarios) == 16
    assert value("cache_hit_rate", scenarios) == metrics["replay.cache_hit_rate"]
    assert value("answer_quality_rate", scenarios) is None  # the judge was off


def test_replay_score_per_gpu():
    metrics, scenarios = _replay()
    assert value("score_per_gpu", scenarios) == pytest.approx(metrics["replay.score"] / 2)


def test_cards_are_recovered_from_card_norm_when_not_given():
    metrics, _ = _replay()
    raw = {k.split(".", 1)[1]: v for k, v in metrics.items() if k.startswith("replay.")}
    assert cards_from_card_norm(raw) == 2
    assert adapt("replay", raw).operating["output_tpm_per_gpu"] == pytest.approx(
        raw["output_tpm"] / 2
    )


def test_a_module_mapped_onto_a_family_uses_its_adapter():
    metrics, _ = _replay()
    renamed = {k.replace("replay.", "traffic_replay.", 1): v for k, v in metrics.items()}
    assert measures_of(renamed, cards=2, aliases={"traffic_replay": "replay"})[0].kind == "replay"
    assert measures_of(renamed, cards=2, aliases={})[0].kind == "quality"


# -- sweeps ------------------------------------------------------------------------


def _sweep_metrics(levels: dict[int, dict], **top) -> dict:
    real = _load("sweep")["metrics"]
    shape = {
        k.split(".", 2)[2]: v for k, v in real.items() if k.startswith("perf_guidellm_sweep.c4.")
    }
    out = {f"perf_guidellm_sweep.{k}": v for k, v in top.items()}
    for c, overrides in levels.items():
        for k, v in {**shape, **overrides}.items():
            out[f"perf_guidellm_sweep.c{c}.{k}"] = v
    return out


def test_a_sweep_operates_at_the_highest_throughput_level_that_met_the_slo():
    metrics = _sweep_metrics(
        {
            1: {"total_tps_mean": 100.0, "output_tps_mean": 10.0, "meets_slo": 1.0},
            8: {"total_tps_mean": 900.0, "output_tps_mean": 80.0, "meets_slo": 1.0},
            16: {
                "total_tps_mean": 800.0,
                "output_tps_mean": 70.0,
                "meets_slo": 1.0,
            },  # past the knee
            32: {
                "total_tps_mean": 1500.0,
                "output_tps_mean": 120.0,
                "meets_slo": 0.0,
            },  # fastest, too slow per request
        },
        reported_concurrency=16.0,
    )
    sc = measures_of(metrics, cards=2)[0]
    assert sc.operating_level == "c8"
    assert value("output_tpm_per_gpu", [sc]) == pytest.approx(80.0 * 60 / 2)
    assert value("meets_slo", [sc]) == 1.0
    # Other points stay addressable.
    assert value("output_tpm:peak", [sc]) == pytest.approx(120.0 * 60)
    assert value("concurrency:c1", [sc]) == 1


def test_a_sweep_with_no_passing_level_reports_its_lowest_level_as_failing_the_slo():
    metrics = _sweep_metrics({4: {"meets_slo": 0.0}, 8: {"meets_slo": 0.0}})
    sc = measures_of(metrics, cards=2)[0]
    assert sc.operating_level == "c4" and value("meets_slo", [sc]) == 0.0


def test_an_auto_sweep_operates_at_its_confirmed_level_not_a_short_probe():
    metrics = _sweep_metrics(
        {
            8: {"total_tps_mean": 900.0, "meets_slo": 1.0},
            12: {"total_tps_mean": 950.0, "meets_slo": 1.0},  # a short probe that happened to pass
        },
        reported_concurrency=8.0,
        search_probes=5.0,
    )
    assert measures_of(metrics, cards=2)[0].operating_level == "c8"


def test_a_sweep_from_before_levels_were_flagged_uses_the_level_llmbench_reported():
    metrics = _sweep_metrics({4: {}, 8: {}}, reported_concurrency=8.0, reported_level_meets_slo=1.0)
    for k in [k for k in metrics if k.endswith(".meets_slo")]:
        del metrics[k]
    sc = measures_of(metrics, cards=2)[0]
    assert sc.operating_level == "c8" and value("meets_slo", [sc]) == 1.0


def test_sweep_input_is_total_minus_output_not_guidellms_overlapping_input_rate():
    metrics = _sweep_metrics(
        {
            4: {
                "total_tps_mean": 1000.0,
                "output_tps_mean": 100.0,
                "input_tps_mean": 5000.0,
                "meets_slo": 1.0,
            }
        }
    )
    assert value("input_tpm", measures_of(metrics, cards=2)) == pytest.approx(900.0 * 60)


def test_sweep_latency_statistics_are_the_ones_guidellm_reports():
    real = _load("sweep")["metrics"]
    scenarios = measures_of(real, cards=2)
    assert value("ttft_ms@p99", scenarios) == real["perf_guidellm_sweep.c4.ttft_p99_ms"]
    assert value("itl_ms@p50", scenarios) == real["perf_guidellm_sweep.c4.itl_p50_ms"]
    assert value("tpot_ms@mean", scenarios) == real["perf_guidellm_sweep.c4.tpot_mean_ms"]
    # A sweep's per-request speed is aggregate ÷ concurrency: a mean, no spread.
    assert (
        value("request_output_tps", scenarios) == real["perf_guidellm_sweep.c4.request_output_tps"]
    )
    assert "request_output_tps@p10" not in scenarios[0].supports
    assert value("success_rate", scenarios) == real["perf_guidellm_sweep.c4.uptime"]


def test_sweep_configured_lengths_are_its_workload():
    metrics = _sweep_metrics({4: {"meets_slo": 1.0}})
    sc = measures_of(
        metrics,
        [{"module": "perf_guidellm_sweep", "params": {"input_tokens": 2048, "output_tokens": 512}}],
        cards=2,
    )
    assert value("prompt_tokens_mean", sc) == 2048 and value("completion_tokens_mean", sc) == 512


# -- the rest ----------------------------------------------------------------------


def test_opencompass_tasks_are_accuracies_and_its_output_tps_is_per_request():
    metrics = {
        "opencompass.gpqa_diamond": 61.5,
        "opencompass.output_tps": 42.0,
        "opencompass.service_tps": 900.0,
        "opencompass.gen_tokens": 12345,
    }
    scenarios = measures_of(metrics)
    assert value("accuracy[gpqa_diamond]", scenarios) == pytest.approx(0.615)
    assert value("request_output_tps", scenarios) == 42.0
    assert value("output_tpm", scenarios) == pytest.approx(900.0 * 60)
    assert value("accuracy[gen_tokens]", scenarios) is None


def test_an_opencompass_score_given_as_a_share_stays_a_share():
    # Current releases report 83 of 100 right as 0.83, older ones as 83.0.
    assert value("accuracy[gpqa_diamond]",
                 measures_of({"opencompass.gpqa_diamond": 0.83})) == pytest.approx(0.83)
    assert value("accuracy[gpqa_diamond]",
                 measures_of({"opencompass.gpqa_diamond": 83.0})) == pytest.approx(0.83)


def test_quality_modules_and_an_unknown_module_keep_their_score():
    metrics = {
        "functional_acceptance.pass_rate": 0.98,
        "functional_acceptance.score": 0.9,
        "brand_new_module.score": 3.0,
        "brand_new_module.thing": 7,
    }
    scenarios = measures_of(metrics)
    assert value("functional_acceptance/pass_rate", scenarios) == 0.98
    assert value("brand_new_module/score", scenarios) == 3.0


def test_the_main_scenario_is_the_first_throughput_one():
    metrics, _ = _replay()
    both = {"functional_acceptance.pass_rate": 1.0, **metrics}
    scenarios = measures_of(both, [{"module": "replay", "params": {"concurrency": 16}}], cards=2)
    assert value("concurrency", scenarios) == 16
    assert value("functional_acceptance/pass_rate", scenarios) == 1.0


def test_a_second_run_of_a_module_is_its_own_scenario():
    metrics, _ = _replay()
    second = {
        k.replace("replay.", "replay#2.", 1): v * 2 if isinstance(v, float) else v
        for k, v in metrics.items()
    }
    scenarios = measures_of({**metrics, **second}, cards=2)
    assert value("replay#2/output_tpm", scenarios) == pytest.approx(
        2 * value("replay/output_tpm", scenarios)
    )


# -- judged by measures ------------------------------------------------------------


def test_an_objective_on_measures_scores_and_judges_a_run():
    from app.evaluation.aggregate import summarize
    from app.measures import Reader

    d = _load("replay")
    reader = Reader(d["metrics"], [{"module": "replay", "params": {"concurrency": 16}}], cards=2)
    objective = {
        "target_metric": "output_tpm_per_gpu",
        "redlines": [
            {"metric": "success_rate", "op": ">=", "value": 0.99},
            {"metric": "ttft_ms@p90[32k_64k]", "op": "<=", "value": 100.0},  # crossed
            {"metric": "replay.uptime", "op": ">=", "value": 0.99},  # a raw key still works
        ],
    }
    summary = summarize(objective, reader)
    assert summary.objective_value == pytest.approx(d["metrics"]["replay.output_tpm"] / 2)
    assert not summary.feasible
    assert summary.breaches == [
        f"ttft_ms@p90[32k_64k]={d['metrics']['replay.ttft_32k_64k_p90_ms']:g} <= 100"
    ]


def test_a_latency_measure_is_minimized_by_default():
    from app.objective import direction

    assert direction({"target_metric": "ttft_ms@p99"}) == "minimize"
    assert direction({"target_metric": "output_tpm_per_gpu"}) == "maximize"


@pytest.mark.parametrize(
    "name, modules, problem",
    [
        ("ttft_ms@p99", ["replay"], None),
        ("itl_ms@p99", ["replay"], "never reports itl_ms@p99"),
        ("request_output_tps@p10", ["perf_guidellm_sweep"], "never reports"),
        ("request_output_tps:c1", ["replay"], "not a sweep"),
        ("request_output_tps:c1", ["perf_guidellm_sweep"], None),
        ("functional_acceptance/pass_rate", ["perf_guidellm_sweep", "functional_acceptance"], None),
        ("hallucination/hallucination_rate", ["replay"], "no module hallucination"),
        ("ttft_ms@p95", ["replay"], "no statistic"),
    ],
)
def test_a_benchmark_that_can_never_report_a_measure_is_caught_before_the_night(
    name, modules, problem
):
    from app.measures import problem_with

    found = problem_with(name, modules, replay_names=(), aliases={})
    assert (found is None) if problem is None else (problem in found)


def test_the_gate_reads_measure_redlines():
    from app.evaluation.gate import Gate

    gate = Gate.from_objective(
        {
            "redlines": [
                {"metric": "ttft_ms@p90", "op": "<=", "value": 3000},
                {"metric": "request_output_tps", "op": ">=", "value": 20},
                {
                    "metric": "ttft_ms@p50[32k_64k]",
                    "op": "<=",
                    "value": 10,
                },  # a bucket is not the gate
            ]
        }
    )
    assert (gate.ttft_ms, gate.ttft_percentile, gate.min_request_output_tps) == (3000, "p90", 20)


def test_an_objective_naming_a_malformed_measure_is_refused_when_saved():
    from fastapi import HTTPException

    from app.api.objectives import _validate
    from app.schemas.core import ObjectiveCreate

    with pytest.raises(HTTPException, match="no statistic 'p95'"):
        _validate(ObjectiveCreate(name="x", target_metric="ttft_ms@p95"))
    _validate(ObjectiveCreate(name="y", target_metric="output_tpm_per_gpu"))
    _validate(ObjectiveCreate(name="z", target_metric="replay.output_tpm_card_norm"))


def test_the_editor_lists_every_measure_and_statistic_as_a_valid_reference():
    from app.measures.catalog import options

    listed = {o["key"]: o for o in options()}
    for key in listed:
        parse(key)  # every listed key is one the platform accepts
    assert "ttft_ms@p99" in listed and "request_output_tps" in listed
    assert "request_output_tps@mean" not in listed  # the default statistic is the bare name
    assert listed["ttft_ms@p99"]["better"] == "lower" and listed["ttft_ms@p99"]["headline"]
    assert not any(k.startswith("accuracy") for k in listed)  # needs a task: typed, not listed


def test_the_run_detail_names_its_objectives_values_in_measures():
    from types import SimpleNamespace

    from app.api.runs import _with_named
    from app.db.models import Result

    d = _load("replay")
    result = Result(id=1, run_id=1, source="llmbench", passed=True, score=None,
                    metrics=d["metrics"], raw={}, feasible=True, breaches=[],
                    created_at=__import__("datetime").datetime(2026, 10, 21))
    campaign = SimpleNamespace(
        objective={"target_metric": "output_tpm_per_gpu",
                   "redlines": [{"metric": "success_rate", "op": ">=", "value": 0.99},
                                {"metric": "itl_ms@p99", "op": "<=", "value": 50}]},
        verify_objective={},
    )
    named = _with_named(result, campaign, SimpleNamespace(config={"tp_size": 2})).named
    assert named["output_tpm_per_gpu"] == pytest.approx(d["metrics"]["replay.output_tpm"] / 2)
    assert named["success_rate"] == d["metrics"]["replay.uptime"]
    assert named["itl_ms@p99"] is None  # a replay never reports it
