"""Regression baseline: LLMBench submission 152, the production-replay benchmark.

A real, finished replay run recorded here so that the second stage of a staged
campaign is checked against metrics that actually came back, not against a
fixture someone invented. Captured 2026-08-04 from the live LLMBench at
`/api/submissions/152`; the metrics below are verbatim.

The submission: benchmark `mh-qwen36-replay-only-v0` against a tp2x2 sglang
router on 4×A100, 2000 real requests at concurrency 3, average prompt 79k
tokens. It took 64 minutes — which is the whole reason this benchmark is a
shortlist stage rather than something every candidate gets.

What it fixes in place:
  - the replay module's metric names, which share nothing with guidellm's
  - the module score is exactly the benchmark's own weighted sum
  - `score_card_norm` is that score expressed per card, derived from the
    normalization factor LLMBench itself applied
  - every replay metric the objective editor offers is one this returned
  - the platform's default verification objective ranks this result
"""

from app.evaluation.aggregate import summarize
from app.evaluation.llmbench import _flatten_metrics
from app.metrics_catalog import DEFAULT_VERIFY_TARGET_METRIC, REPLAY

# -- the module's metrics, verbatim -------------------------------------------

REPLAY_METRICS: dict = {
    'attempted_requests': 2000,
    'avg_completion_tokens': 376.863,
    'avg_prompt_tokens': 79162.2295,
    'cache_hit_rate': 0.6853195563422073,
    'cached_tpm': 1809389.4421011726,
    'cached_tpm_card_norm': 3618778.8842023453,
    'error_class_counts': {},
    'error_rate': 0.0,
    'error_requests': 0,
    'finish_marker_counts': {
        'openai_finish_reason_stop': 276,
        'openai_finish_reason_length': 14,
        'openai_finish_reason_tool_calls': 1710,
    },
    'finish_reason_length_count': 14,
    'finish_reason_stop_count': 276,
    'generation_capped_count': 0,
    'http_200_count': 2000,
    'image_4xx_errors': 0,
    'image_requests_total': 0,
    'input_tpm': 2640212.766958707,
    'input_tpm_card_norm': 5280425.533917414,
    'input_tps_mean': 59678.15035142457,
    'input_tps_p10': 16116.940545124155,
    'input_tps_p50': 51253.28417041058,
    'input_tps_p90': 117092.16513891716,
    'judge_answered_count': 52,
    'judge_disconnect_count': 0,
    'judge_error_count': 0,
    'judge_failures': 0,
    'judge_good_acc_rate': 0.9858299595141701,
    'judge_halluc_clear_rate': 0.006072874493927126,
    'judge_halluc_suspected_count': 15,
    'judge_kept_count': 494,
    'judge_length_count': 6,
    'judge_poor_rate': 0.01417004048582996,
    'judge_sampled_count': 500,
    'judge_toolcall_count': 442,
    'multi_system_4xx_errors': 0,
    'multi_system_requests': 0,
    'not_started_requests': 0,
    'output_tpm': 12569.106634299116,
    'output_tpm_card_norm': 25138.213268598232,
    'output_tps_avg': 71.00452216392199,
    'output_tps_mean': 71.00452216392199,
    'output_tps_p10': 17.80668522149669,
    'output_tps_p50': 68.99745286561355,
    'output_tps_p90': 123.50770576185292,
    'repetitive_token_count': 0,
    'successful_requests': 2000,
    'system_normalized_count': 0,
    'tokens_after_finish_count': 0,
    'tool_call_4xx_errors': 0,
    'tool_requests_total': 1931,
    'total_cached_tokens': 108502848,
    'total_input_tokens': 158324459,
    'total_output_tokens': 753726,
    'total_requests': 2000,
    'total_time_p50_ms': 3296.049242839217,
    'total_time_p99_ms': 28878.323812186718,
    'total_tpm_card_norm': 5305563.747186013,
    'total_uncached_input_tokens': 49821611,
    'ttft_128k_256k_avg_ms': 5019.58406812088,
    'ttft_128k_256k_count': 275,
    'ttft_128k_256k_p50_ms': 1792.708458378911,
    'ttft_128k_256k_p90_ms': 12040.616927668452,
    'ttft_16k_32k_avg_ms': 864.8714300356249,
    'ttft_16k_32k_count': 335,
    'ttft_16k_32k_p50_ms': 585.8233720064163,
    'ttft_16k_32k_p90_ms': 1058.6934231221676,
    'ttft_32k_64k_avg_ms': 1490.8880476068039,
    'ttft_32k_64k_count': 397,
    'ttft_32k_64k_p50_ms': 778.3722057938576,
    'ttft_32k_64k_p90_ms': 2795.308769121767,
    'ttft_64k_128k_avg_ms': 2616.016184510584,
    'ttft_64k_128k_count': 944,
    'ttft_64k_128k_p50_ms': 1201.0763110592961,
    'ttft_64k_128k_p90_ms': 6477.8291292488575,
    'ttft_6k_16k_avg_ms': 610.7447565346956,
    'ttft_6k_16k_count': 25,
    'ttft_6k_16k_p50_ms': 522.9440163820982,
    'ttft_6k_16k_p90_ms': 966.7000681161885,
    'ttft_ge_256k_avg_ms': None,
    'ttft_ge_256k_count': 0,
    'ttft_ge_256k_p50_ms': None,
    'ttft_ge_256k_p90_ms': None,
    'ttft_input_unknown_count': 0,
    'ttft_lt_6k_avg_ms': 783.067406155169,
    'ttft_lt_6k_count': 8,
    'ttft_lt_6k_p50_ms': 242.97468271106482,
    'ttft_lt_6k_p90_ms': 1576.537399366497,
    'ttft_mean_ms': 2395.6918039494344,
    'ttft_p50_ms': 1052.491963841021,
    'ttft_p90_ms': 6638.121579959989,
    'ttft_p99_ms': 13348.45928659663,
    'uncached_input_tpm': 830823.3248575341,
    'unfinished_rate': 0.0,
    'unfinished_requests': 0,
    'uptime': 1.0,
    'wall_time_s': 3597.9931840654463,
}

# The benchmark's score-role weights, from its metric_configs: the module score
# is 1.8*uncached_input_tpm + 0.36*cached_tpm + 10.8*output_tpm. Recorded to
# show `score` is not an opaque number but a value-per-minute figure.
MODULE_SCORE = 2282608.535550414

SUBMISSION: dict = {
    "id": 152,
    "benchmark_slug": "mh-qwen36-replay-only-v0",
    "status": "done",
    "passed": False,  # cache_hit_rate 0.685 against a >= 0.8 redline
    "score_total": MODULE_SCORE,
    "cards_per_machine": 4,
    "machine_count": 1,
    "card_type": "A100",
    "runs": [
        {
            "module_name": "replay_prod",
            "status": "done",
            "score": MODULE_SCORE,
            "passed": False,
            "metrics_json": REPLAY_METRICS,
        }
    ],
}


def _metrics() -> dict:
    return _flatten_metrics(SUBMISSION)


# -- the shape of what comes back ---------------------------------------------


def test_the_replay_module_flattens_to_its_own_namespace():
    """Nothing here is nested. guidellm reports one metric set per concurrency
    level; replay reports one set, period — so a `<module>.<level>.<metric>`
    key that works for the sweep has no counterpart at all here."""
    metrics = _metrics()
    assert metrics[f"{REPLAY}.output_tpm"] == REPLAY_METRICS["output_tpm"]
    assert metrics[f"{REPLAY}.cache_hit_rate"] == REPLAY_METRICS["cache_hit_rate"]
    assert not [k for k in metrics if k.startswith(f"{REPLAY}.c1.")]


def test_the_dict_valued_metrics_survive_one_level_down():
    """`finish_marker_counts` is a dict of counters. Flattened one level it
    becomes addressable; left nested it is invisible to any objective."""
    metrics = _metrics()
    assert metrics[f"{REPLAY}.finish_marker_counts.openai_finish_reason_tool_calls"] == 1710


def test_the_score_is_the_benchmarks_own_weighted_sum():
    """Not a normalized 0-1 figure: `passthrough_scaled` terms bypass the
    weight normalization, so the score is value-per-minute in raw units."""
    expected = (
        1.8 * REPLAY_METRICS["uncached_input_tpm"]
        + 0.36 * REPLAY_METRICS["cached_tpm"]
        + 10.8 * REPLAY_METRICS["output_tpm"]
    )
    assert abs(expected - MODULE_SCORE) < 1e-6
    assert _metrics()[f"{REPLAY}.score"] == MODULE_SCORE


# -- the derived per-card score -----------------------------------------------


def test_the_score_is_also_published_per_card():
    """The raw score is a sum of absolute token rates, so it rewards a config
    for occupying more GPUs. This one asks what a single card bought.

    The factor is recovered from a pair LLMBench itself returned — here
    input_tpm_card_norm / input_tpm, which is 8/4 for this 4-card endpoint —
    rather than by dividing by a hardcoded baseline.
    """
    scale = REPLAY_METRICS["input_tpm_card_norm"] / REPLAY_METRICS["input_tpm"]
    assert scale == 2.0, "8-card baseline, 4-card endpoint"
    assert _metrics()[f"{REPLAY}.score_card_norm"] == MODULE_SCORE * 2.0


def test_a_module_with_no_normalized_metrics_publishes_no_per_card_score():
    """Silence beats a made-up number: a benchmark that never normalizes has
    not told us how many cards were involved, and guessing 8 would rank a
    4-card config as though it were twice as good as it is."""
    bare = {
        "runs": [
            {"module_name": "odd", "score": 5.0, "metrics_json": {"output_tpm": 10.0}}
        ]
    }
    metrics = _flatten_metrics(bare)
    assert metrics["odd.score"] == 5.0
    assert "odd.score_card_norm" not in metrics


def test_a_zero_valued_pair_does_not_become_a_division_by_zero():
    """An idle module can report 0.0 for a rate and 0.0 for its normalized
    twin. Dividing there is a crash in the middle of a night."""
    bare = {
        "runs": [
            {
                "module_name": "odd",
                "score": 5.0,
                "metrics_json": {
                    "a_tpm": 0.0, "a_tpm_card_norm": 0.0,
                    "b_tpm": 10.0, "b_tpm_card_norm": 20.0,
                },
            }
        ]
    }
    assert _flatten_metrics(bare)["odd.score_card_norm"] == 10.0


# -- the catalog promise ------------------------------------------------------


REAL_KEYS = {f"{REPLAY}.{key}" for key in REPLAY_METRICS} | {
    f"{REPLAY}.score",
    # Ours, derived above from two keys LLMBench returned.
    f"{REPLAY}.score_card_norm",
}


def test_every_offered_replay_metric_exists_in_a_real_replay_result():
    """A catalog entry is a promise that an objective built on it will work.

    The replay module and the guidellm sweep share not one metric name, so an
    entry copied from one to the other scores every run None and finishes the
    campaign reporting that nothing held its redlines.
    """
    from app.metrics_catalog import METRICS

    offered = {m["key"] for m in METRICS if m["key"].startswith(f"{REPLAY}.")}
    assert offered, "the editor offers no replay metrics at all"
    assert offered <= REAL_KEYS, f"offered but never returned: {sorted(offered - REAL_KEYS)}"


def test_the_default_verification_objective_scores_a_real_result():
    """The campaign default has to work against this payload untouched — it is
    what a campaign gets when it turns verification on and says nothing more."""
    summary = summarize({"target_metric": DEFAULT_VERIFY_TARGET_METRIC}, _metrics())
    assert summary.objective_value == MODULE_SCORE * 2.0
    assert summary.feasible


def test_a_replay_objective_expresses_the_latency_ladder():
    """Replay grades TTFT by input length, because a 64k prompt cannot be held
    to a 6k prompt's deadline. A single overall TTFT redline cannot say that."""
    objective = {
        "target_metric": f"{REPLAY}.score_card_norm",
        "redlines": [
            {"metric": f"{REPLAY}.ttft_32k_64k_p90_ms", "op": "<=", "value": 15000},
            {"metric": f"{REPLAY}.uptime", "op": ">=", "value": 0.99},
        ],
    }
    assert summarize(objective, _metrics()).feasible, "2795ms and uptime 1.0 both hold"

    tighter = {**objective, "redlines": [
        {"metric": f"{REPLAY}.ttft_32k_64k_p90_ms", "op": "<=", "value": 2000},
    ]}
    breached = summarize(tighter, _metrics())
    assert not breached.feasible
    assert "ttft_32k_64k_p90_ms" in breached.breaches[0]
