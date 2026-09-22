"""Regression baseline: LLMBench submission 357, a replay run the platform
*failed* — and the reasons it failed for.

Captured 2026-08-06 from the live LLMBench at `/api/submissions/357` (run 216
of campaign 27). The endpoint was effectively unreachable during the replay —
uptime 6.5%, 706 of 755 requests dropped on connection errors — so it breached
its uptime redline massively and several TTFT ceilings besides.

LLMBench records that verdict as `passed: False` with no accompanying text: the
breached thresholds live in `metric_configs_json` (role "redline", a
min_val/max_val bound) checked against `metrics_json`. Before this was parsed,
the whole reason collapsed to "benchmark ran but did not pass (score …)". This
pins the recovery in place: the config metadata and the values below are
verbatim, trimmed to the metrics that carry a bound.
"""

from app.evaluation.llmbench import (
    _describe_module_failure,
    _failed_modules,
    _fmt_num,
    _redline_breaches,
)

# -- the bounded configs, verbatim (role/key/bound), real order ---------------
# Redlines gate the run; the two `display` bounds are shown on the platform but
# do NOT fail it — a distinction the parser has to honour.
METRIC_CONFIGS: list[dict] = [
    {"key": "uptime", "role": "redline", "min_val": 0.99, "max_val": None},
    {"key": "ttft_p99_ms", "role": "display", "min_val": None, "max_val": 10000},
    {"key": "total_time_p99_ms", "role": "display", "min_val": None, "max_val": 120000},
    {"key": "unfinished_rate", "role": "redline", "min_val": None, "max_val": 0.05},
    {"key": "input_tps_mean", "role": "redline", "min_val": 15, "max_val": None},
    {"key": "cache_hit_rate", "role": "redline", "min_val": 0.6, "max_val": None},
    # These two redlines never got a value — judging never ran. Skipped, not guessed.
    {"key": "judge_poor_rate", "role": "redline", "min_val": None, "max_val": 0.1},
    {"key": "judge_halluc_clear_rate", "role": "redline", "min_val": None, "max_val": 0.02},
    {"key": "ttft_lt_6k_p90_ms", "role": "redline", "min_val": None, "max_val": 5000},
    {"key": "ttft_lt_6k_p50_ms", "role": "redline", "min_val": None, "max_val": 2000},
    {"key": "ttft_lt_6k_avg_ms", "role": "redline", "min_val": None, "max_val": 2000},
    {"key": "ttft_16k_32k_p90_ms", "role": "redline", "min_val": None, "max_val": 8000},
    {"key": "ttft_16k_32k_p50_ms", "role": "redline", "min_val": None, "max_val": 4000},
    {"key": "ttft_16k_32k_avg_ms", "role": "redline", "min_val": None, "max_val": 6000},
    {"key": "ttft_32k_64k_p90_ms", "role": "redline", "min_val": None, "max_val": 15000},
]

# -- the values those configs point at, verbatim ------------------------------
METRICS: dict = {
    "uptime": 0.06490066225165562,
    "ttft_p99_ms": 11611.298897564411,
    "total_time_p99_ms": 57433.079685270786,
    "unfinished_rate": 0.0,
    "input_tps_mean": 46507.484349440805,
    "cache_hit_rate": 0.6913556680020979,
    "judge_poor_rate": None,
    "judge_halluc_clear_rate": None,
    "ttft_lt_6k_p90_ms": 6323.75049777329,
    "ttft_lt_6k_p50_ms": 322.0968712121248,
    "ttft_lt_6k_avg_ms": 2320.15184375147,
    "ttft_16k_32k_p90_ms": 9138.554196804762,
    "ttft_16k_32k_p50_ms": 499.4145594537258,
    "ttft_16k_32k_avg_ms": 2293.0569211867723,
    "ttft_32k_64k_p90_ms": 4807.511354982856,
}

MODULE: dict = {
    "module_name": "replay_prod",
    "status": "done",
    "passed": False,
    "error": None,
    "score": 1854229.6198796327,
    "metric_configs_json": METRIC_CONFIGS,
    "metrics_json": METRICS,
}

SUBMISSION: dict = {
    "id": 357,
    "status": "done",
    "passed": False,
    "score_total": 1854229.6198796327,
    "runs": [MODULE],
}

# The four redlines this run actually broke, in config order.
EXPECTED = [
    "uptime 0.0649 < 0.99",
    "ttft_lt_6k_p90_ms 6324 > 5000",
    "ttft_lt_6k_avg_ms 2320 > 2000",
    "ttft_16k_32k_p90_ms 9139 > 8000",
]


def test_the_breached_redlines_are_recovered_actual_against_limit():
    """The whole point: 'did not pass' becomes the specific thresholds broken."""
    assert _redline_breaches(MODULE) == EXPECTED


def test_a_display_threshold_is_not_reported_as_a_reason():
    """ttft_p99_ms (11611) is over its 10000 display bound, but `display` does
    not gate the run — naming it would invent a reason LLMBench did not act on."""
    breaches = _redline_breaches(MODULE)
    assert not any("ttft_p99_ms" in b for b in breaches)


def test_a_redline_without_a_measured_value_is_skipped_not_guessed():
    """judge_poor_rate and judge_halluc_clear_rate are redlines whose metric
    came back None — judging never ran. Absence is not a breach."""
    breaches = _redline_breaches(MODULE)
    assert not any(b.startswith("judge_") for b in breaches)


def test_a_satisfied_redline_never_appears():
    """cache_hit_rate 0.691 >= 0.60 and unfinished_rate 0.0 <= 0.05 both hold."""
    breaches = _redline_breaches(MODULE)
    assert not any(b.startswith("cache_hit_rate") for b in breaches)
    assert not any(b.startswith("unfinished_rate") for b in breaches)


def test_the_module_verdict_names_module_and_reasons():
    assert _failed_modules(SUBMISSION) == "replay_prod (" + "; ".join(EXPECTED) + ")"


def test_a_long_breach_list_is_truncated_with_a_count():
    """A broadly-unhealthy run can break a dozen per-bucket TTFT ceilings; the
    verdict spells out the first few and counts the rest."""
    many = {
        "metric_configs_json": [
            {"key": f"m{i}", "role": "redline", "min_val": None, "max_val": 1}
            for i in range(9)
        ],
        "metrics_json": {f"m{i}": 5 for i in range(9)},
    }
    breaches = _redline_breaches(many)
    assert len(breaches) == 9
    detail = _describe_module_failure(many)
    assert detail.endswith("; +3 more")  # 9 breaches, 6 shown
    assert detail.count(" > 1") == 6


def test_an_explicit_error_wins_over_derived_breaches():
    """When the platform gives us its own words (a module that crashed), those
    are more specific than any threshold arithmetic."""
    crashed = {**MODULE, "error": "CUDA out of memory\nsecond line"}
    assert _describe_module_failure(crashed) == "CUDA out of memory"


def test_a_submission_without_config_metadata_still_falls_back_to_score():
    """Older/other submissions (e.g. the 152 fixture) carry no
    metric_configs_json. The reason then is the score, exactly as before —
    this change adds detail where it exists and removes none where it does not."""
    bare = {"module_name": "replay_prod", "passed": False, "score": 42.0}
    assert _describe_module_failure(bare) == "score 42.0"
    assert _redline_breaches(bare) == []


def test_number_formatting_stays_short_and_lossless_enough():
    assert _fmt_num(5000.0) == "5000"       # whole numbers lose the decimal
    assert _fmt_num(0.99) == "0.99"
    assert _fmt_num(0.06490066225165562) == "0.0649"
    assert _fmt_num(6323.75049777329) == "6324"
