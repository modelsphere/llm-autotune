"""The morning report. Its job is to answer, in the first paragraph: did
anything beat what we already run?"""

from types import SimpleNamespace

from app.reporting import render_campaign_report


def _campaign(**kw):
    return SimpleNamespace(
        name="node-24 tp sweep",
        objective={"target_metric": "perf.output_tpm_card_norm"},
        model_path="/models/qwen",
        engine="sglang",
        image="registry.example.com/sglang:v1",
        **kw,
    )


def _run(run_id, kind, status, config, failure_class="", error=""):
    return SimpleNamespace(
        id=run_id,
        kind=kind,
        status=status,
        failure_class=failure_class,
        error=error,
        machine_id=1,
        candidate=SimpleNamespace(config=config),
    )


def _result(run_id, value):
    return SimpleNamespace(run_id=run_id, metrics={"perf.output_tpm_card_norm": value})


MACHINES = {1: SimpleNamespace(name="node-24")}


def test_only_the_varying_dimensions_are_shown():
    """A campaign's config is mostly a fixed base; printing all of it buries
    the one knob being compared (the first real report was unreadable)."""
    base = {"context_length": 262144, "page_size": 64, "mem_fraction_static": 0.9}
    runs = [
        _run(1, "experiment", "succeeded", {**base, "tp": 2}),
        _run(2, "experiment", "succeeded", {**base, "tp": 4}),
    ]
    results = {1: _result(1, 50000.0), 2: _result(2, 30000.0)}
    report = render_campaign_report(_campaign(), runs, results, MACHINES)

    assert "tp=2" in report and "tp=4" in report
    assert "context_length" not in report, "fixed base args are noise here"
    assert "page_size" not in report


def test_single_candidate_still_shows_its_config():
    runs = [_run(1, "experiment", "succeeded", {"tp": 2, "page_size": 64})]
    report = render_campaign_report(_campaign(), runs, {1: _result(1, 1.0)}, MACHINES)
    assert "page_size=64" in report, "with nothing to contrast, show the whole config"


def test_winner_is_stated_relative_to_production():
    runs = [
        _run(1, "baseline", "succeeded", {"__baseline__": "prod"}),
        _run(2, "experiment", "succeeded", {"tp": 2}),
        _run(3, "experiment", "succeeded", {"tp": 4}),
    ]
    results = {1: _result(1, 50000.0), 2: _result(2, 60000.0), 3: _result(3, 45000.0)}
    report = render_campaign_report(_campaign(), runs, results, MACHINES)

    assert "tp=2" in report.split("## Results")[0], "the winner belongs in the verdict"
    assert "+20.0%" in report
    assert "production (as handed over)" in report


def test_a_sub_noise_difference_is_not_called_a_win():
    """Two identical configs measured 0.2% apart on real hardware; reporting
    that as an improvement would be tuning theatre."""
    runs = [
        _run(1, "baseline", "succeeded", {"__baseline__": "prod"}),
        _run(2, "experiment", "succeeded", {"tp": 2}),
    ]
    results = {1: _result(1, 50000.0), 2: _result(2, 50100.0)}  # +0.2%, inside noise
    report = render_campaign_report(_campaign(), runs, results, MACHINES)

    assert "No meaningful difference" in report
    assert "beats production" not in report
    assert "noise" in report


def test_report_says_so_when_nothing_beats_production():
    runs = [
        _run(1, "baseline", "succeeded", {"__baseline__": "prod"}),
        _run(2, "experiment", "succeeded", {"tp": 2}),
    ]
    results = {1: _result(1, 50000.0), 2: _result(2, 40000.0)}
    report = render_campaign_report(_campaign(), runs, results, MACHINES)
    assert "nothing beat production" in report
    assert "-20.0%" in report


def test_failures_are_explained_and_turned_into_advice():
    runs = [
        _run(1, "experiment", "failed", {"tp": 8}, failure_class="oom", error="CUDA OOM\nmore"),
        _run(2, "experiment", "failed", {"tp": 2}, failure_class="ssh_timeout", error="timeout"),
    ]
    report = render_campaign_report(_campaign(), runs, {}, MACHINES)
    assert "`oom`" in report
    assert "mem_fraction_static" in report  # actionable advice, not just the error
    assert "infrastructure, not configuration" in report  # ssh_timeout deserves a retry


def test_missing_baseline_is_called_out():
    runs = [_run(1, "experiment", "succeeded", {"tp": 2})]
    report = render_campaign_report(_campaign(), runs, {1: _result(1, 1.0)}, MACHINES)
    assert "No baseline canary ran" in report


def test_no_successful_runs_does_not_pretend_otherwise():
    runs = [_run(1, "experiment", "failed", {"tp": 2}, failure_class="oom")]
    report = render_campaign_report(_campaign(), runs, {}, MACHINES)
    assert "No successful runs to rank." in report
