"""One benchmark, one module, run twice.

LLMBench lets a benchmark list the same module more than once with different
params — sweep-test runs `perf_guidellm_sweep` at 50k+1.5k input/output and
again at 40k+300 — and reports every run under the module's name. Namespacing
metrics by that name alone made the second run silently overwrite the first's
every number, and the measurement's module reports listed two identical
entries with nothing saying which was which.
"""

from types import SimpleNamespace

from app.evaluation.llmbench import (
    LLMBenchClient,
    _flatten_metrics,
    instance_keys,
    module_reports,
)

_FIRST = {"input_tokens": 50000, "output_tokens": 1500, "search_mode": "auto",
          "concurrencies": "1,2,4,8,16", "fixed_level": None, "processor_path": None}
_SECOND = {"input_tokens": 40000, "output_tokens": 300, "search_mode": "auto",
           "concurrencies": "1,2,4,8,16", "fixed_level": None, "processor_path": None}


def _run(params, output_tps, score, passed=True):
    return {
        "module_name": "perf_guidellm_sweep", "status": "done", "passed": passed,
        "score": score, "params_json": params,
        "metrics_json": {
            "output_tps": output_tps, "reported_concurrency": 8,
            "c8": {"output_tps_mean": output_tps, "request_output_tps": output_tps / 8},
        },
        "metric_configs_json": [{"key": "uptime", "role": "redline", "min_val": 0.9}],
    }


_SUBMISSION = {
    "status": "done", "passed": True, "score_total": 1.5,
    "runs": [_run(_FIRST, 595.0, 1.5), _run(_SECOND, 170.0, 0.4)],
}


def test_the_first_run_keeps_the_bare_name_and_later_runs_are_numbered():
    # Every existing objective and the metrics catalog address
    # `perf_guidellm_sweep.<metric>`; they must keep meaning the first run.
    assert instance_keys(["functional_acceptance", "perf_guidellm_sweep"]) == [
        "functional_acceptance", "perf_guidellm_sweep",
    ]
    assert instance_keys(["perf_guidellm_sweep", "perf_guidellm_sweep", "replay_prod",
                          "perf_guidellm_sweep"]) == [
        "perf_guidellm_sweep", "perf_guidellm_sweep#2", "replay_prod",
        "perf_guidellm_sweep#3",
    ]


def test_two_runs_of_one_module_flatten_into_distinct_namespaces():
    metrics = _flatten_metrics(_SUBMISSION)

    assert metrics["perf_guidellm_sweep.output_tps"] == 595.0
    assert metrics["perf_guidellm_sweep#2.output_tps"] == 170.0
    assert metrics["perf_guidellm_sweep.c8.request_output_tps"] == 595.0 / 8
    assert metrics["perf_guidellm_sweep#2.c8.request_output_tps"] == 170.0 / 8
    assert metrics["perf_guidellm_sweep.score"] == 1.5
    assert metrics["perf_guidellm_sweep#2.score"] == 0.4
    # The module a key belongs to is still whatever precedes the first dot.
    assert {k.split(".")[0] for k in metrics} == {
        "score_total", "perf_guidellm_sweep", "perf_guidellm_sweep#2",
    }


def test_module_reports_tell_the_two_runs_apart_and_freeze_their_params():
    first, second = module_reports(_SUBMISSION)

    assert (first["module"], second["module"]) == ("perf_guidellm_sweep", "perf_guidellm_sweep#2")
    assert first["module_name"] == second["module_name"] == "perf_guidellm_sweep"
    # What was measured, on the record — the benchmark can be edited later.
    assert first["params"]["input_tokens"] == 50000 and first["params"]["output_tokens"] == 1500
    assert second["params"]["input_tokens"] == 40000 and second["params"]["output_tokens"] == 300
    assert second["params"]["search_mode"] == "auto"
    # Unset defaults say nothing and are not frozen.
    assert "fixed_level" not in first["params"] and "processor_path" not in first["params"]
    assert first["score"] == 1.5 and second["score"] == 0.4


def test_a_run_without_params_reports_none_rather_than_crashing():
    submission = {"status": "done", "runs": [{"module_name": "perf_mock", "status": "done",
                                              "passed": True, "metrics_json": {"x": 1}}]}
    (report,) = module_reports(submission)
    assert report["module"] == "perf_mock" and report["params"] == {}


def test_the_failed_module_verdict_names_the_run_that_failed():
    from app.evaluation.llmbench import _failed_modules

    submission = {"runs": [_run(_FIRST, 595.0, 1.5), _run(_SECOND, 170.0, 0.4, passed=False)]}
    submission["runs"][1]["error"] = "uptime 0.5 < 0.9"
    assert _failed_modules(submission) == "perf_guidellm_sweep#2 (uptime 0.5 < 0.9)"


def test_the_benchmark_catalog_lists_instance_keys_so_objectives_on_them_validate():
    # The submit-time check reads a target metric's module off this catalog;
    # `perf_guidellm_sweep#2.output_tpm_card_norm` must validate the same way
    # the harvest will report it.
    client = LLMBenchClient(base_url="http://llmbench.test/api", api_key="k")
    payload = {"benchmarks": [
        {"slug": "sweep-test", "modules": [
            {"module_name": "perf_guidellm_sweep"}, {"module_name": "perf_guidellm_sweep"},
        ]},
        {"slug": "replay", "modules": [{"module_name": "replay_prod"}]},
    ]}
    client._send = lambda *a, **k: SimpleNamespace(  # type: ignore[method-assign]
        raise_for_status=lambda: None, json=lambda: payload,
    )
    assert client.modules_by_benchmark() == {
        "sweep-test": ["perf_guidellm_sweep", "perf_guidellm_sweep#2"],
        "replay": ["replay_prod"],
    }
