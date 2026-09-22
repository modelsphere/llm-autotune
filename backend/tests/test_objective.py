"""Objectives are "maximize X subject to Y" — the redline half decides
which of two configs is actually the answer.

Concretely, on node-24: tp=4 gave 27% lower TTFT but 36% worse per-card
throughput than tp=2. Which one wins is entirely a question of the SLO.
"""

from types import SimpleNamespace

from app.objective import (
    breaches,
    direction,
    holds_redlines,
    improvement_pct,
    target_metric,
)
from app.reporting import render_campaign_report

SLO = {
    "target_metric": "tpm_card",
    "redlines": [
        {"metric": "ttft_p99_ms", "op": "<=", "value": 7000},
        {"metric": "pass_rate", "op": ">=", "value": 0.99},
    ],
}

TP2 = {"tpm_card": 68425.0, "ttft_p99_ms": 8692.0, "pass_rate": 1.0}
TP4 = {"tpm_card": 43607.0, "ttft_p99_ms": 6324.0, "pass_rate": 1.0}


def test_target_metric_falls_back_to_per_card_throughput():
    """The platform default is per-card throughput, not LLMBench's composite
    score — a composite shifts whenever the benchmark's weights are edited,
    which silently breaks cross-night comparison."""
    assert target_metric(None) == "perf_guidellm_sweep.output_tpm_card_norm"
    assert target_metric({"target_metric": "x"}) == "x"


def test_direction_is_inferred_from_the_metric():
    """Picking a latency metric must not rank the slowest config first."""
    assert direction({"target_metric": "perf_guidellm_sweep.ttft_p99_ms"}) == "minimize"
    assert direction({"target_metric": "perf_guidellm_sweep.output_tps"}) == "maximize"
    # an explicit choice always wins
    assert direction(
        {"target_metric": "perf_guidellm_sweep.ttft_p99_ms", "direction": "maximize"}
    ) == "maximize"


def test_improvement_is_positive_when_a_minimized_metric_drops():
    """27% lower TTFT is an improvement; reporting it as -27% reads as a
    regression."""
    faster = {"target_metric": "perf_guidellm_sweep.ttft_p99_ms", "direction": "minimize"}
    assert improvement_pct(6324.0, 8692.0, faster) > 0
    assert improvement_pct(9000.0, 8692.0, faster) < 0
    more = {"target_metric": "perf_guidellm_sweep.output_tps", "direction": "maximize"}
    assert improvement_pct(363.4, 285.1, more) > 0


def test_the_slo_decides_which_config_is_eligible():
    # tp=2 wins the target metric but breaches the latency SLO
    assert not holds_redlines(SLO, TP2)
    assert "ttft_p99_ms=8692" in breaches(SLO, TP2)[0]
    # tp=4 is slower per card but satisfies it
    assert holds_redlines(SLO, TP4)


def test_a_missing_metric_crosses_its_redline():
    """We cannot certify an SLO we did not measure; passing silently would
    promote exactly the configs whose benchmark omitted the number."""
    assert not holds_redlines(SLO, {"tpm_card": 1.0, "pass_rate": 1.0})
    assert "missing" in breaches(SLO, {"tpm_card": 1.0, "pass_rate": 1.0})[0]


def test_no_redlines_means_everything_qualifies():
    assert holds_redlines({"target_metric": "tpm_card"}, TP2)
    assert holds_redlines(None, {})


def _run(run_id, config, kind="experiment"):
    return SimpleNamespace(
        id=run_id, kind=kind, status="succeeded", failure_class="", error="",
        machine_id=1, candidate=SimpleNamespace(config=config),
    )


def test_report_will_not_crown_a_config_that_breaches_the_slo():
    campaign = SimpleNamespace(
        name="tp sweep", objective=SLO, model_path="/m", engine="sglang", image="i"
    )
    runs = [_run(1, {"tp": 2}), _run(2, {"tp": 4})]
    results = {
        1: SimpleNamespace(run_id=1, metrics=TP2),
        2: SimpleNamespace(run_id=2, metrics=TP4),
    }
    report = render_campaign_report(campaign, runs, results, {1: SimpleNamespace(name="node-24")})

    verdict = report.split("## Results")[0]
    assert "tp=4" in verdict, "the eligible config leads, despite the lower target value"
    assert "crossed one of the objective's redlines" in report
    assert "ttft_p99_ms=8692" in report  # says exactly what tp=2 breached


def test_campaigns_written_before_the_rename_still_rank_the_same_way():
    """A night that already ran carries its limits under the old key. Reading
    only the new one would silently promote a config the user rejected."""
    legacy = {"target_metric": "tpm_card", "constraints": SLO["redlines"]}
    assert not holds_redlines(legacy, TP2)
    assert holds_redlines(legacy, TP4)
