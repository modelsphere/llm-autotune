"""Metrics a benchmark reports, so an objective can be built by picking.

Keys are LLMBench's own names, verbatim, namespaced by module
(`<module>.<metric>`, and `<module>.<level>.<metric>` for the per-concurrency
groups). Deliberately not renamed into a vocabulary of our own: the benchmark
platform is where these are defined, a run's `results.raw` is the record, and a
key that reads differently here than it does there is a key someone has to
translate every time they compare the two.

Only `label` and `help` are ours; they are display text, not identity.

`better` records which direction is an improvement — without it the platform
silently assumes higher is better and cannot express "minimize TTFT".

An entry here is a promise that an objective built on it will work. Adding one
LLMBench does not return means a campaign scores every run None, marks them all
infeasible, and reports "nothing stayed inside the redlines" after a full night
— see tests/test_regression_campaign20.py, which pins this list against the
keys a real campaign actually produced.
"""

from typing import Any, Literal

Better = Literal["higher", "lower"]

# The benchmark a metric comes from, as an operator would name it.
SWEEP_GROUP = "Synthetic sweep"
REPLAY_GROUP = "Production replay"
OVERALL_GROUP = "Whole submission"


def _m(
    key: str,
    label: str,
    better: Better,
    help_: str,
    *,
    unit: str = "",
    headline: bool = False,
    group: str = SWEEP_GROUP,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "better": better,
        "help": help_,
        "unit": unit,
        "headline": headline,
        # Which benchmark reports it. The two stages of a staged campaign
        # report disjoint metric names, so an editor that lists them together
        # invites building an objective out of keys the chosen benchmark will
        # never return.
        "group": group,
    }


PERF = "perf_guidellm_sweep"
FUNC = "functional_acceptance"
REPLAY = "replay_prod"

METRICS: list[dict[str, Any]] = [
    # -- throughput, card-normalized -----------------------------------------
    # The right default family. An absolute rate almost always rewards giving a
    # config more GPUs — eight cards beat four at nearly anything — so ranking
    # on it turns a parameter search into "ask for more hardware". Dividing by
    # the cards a config occupies asks the question actually worth answering:
    # what does one GPU buy you?
    _m(f"{PERF}.output_tpm_card_norm", "Output TPM per card", "higher",
       "Output tokens/min divided by the GPUs the config occupies. The fair way "
       "to compare tp=2 against tp=4. The platform default.",
       unit="tok/min/GPU", headline=True),
    _m(f"{PERF}.total_tpm_card_norm", "Total TPM per card", "higher",
       "Input + output tokens/min per GPU. Use when prefill is a real part of "
       "the cost, not an afterthought.", unit="tok/min/GPU", headline=True),
    _m(f"{PERF}.input_tpm_card_norm", "Input TPM per card", "higher",
       "Prefill tokens/min per GPU. The one to rank on for long-context or "
       "heavily prompt-bound traffic.", unit="tok/min/GPU", headline=True),

    # -- throughput, absolute ------------------------------------------------
    _m(f"{PERF}.output_tps", "Output tokens/s", "higher",
       "Absolute decode throughput of one service. Favours wider parallelism, "
       "which also costs more GPUs — prefer the card-normalized form unless "
       "you are sizing a fixed deployment.", unit="tok/s"),
    _m(f"{PERF}.input_tps", "Input tokens/s", "higher",
       "Prefill throughput — the dominant cost for long-context workloads.",
       unit="tok/s"),
    _m(f"{PERF}.total_tps_mean", "Total tokens/s (mean)", "higher",
       "Input + output combined. guidellm reports this as a distribution, not "
       "a single number — there is no plain `total_tps` key.", unit="tok/s"),

    # -- single-stream (per-request) -----------------------------------------
    # output_tps divided by the concurrency it was measured at: the rate ONE
    # stream sees. The natural floor to put a minimum on, because a config can
    # win on aggregate throughput by batching so hard that any single request
    # crawls — fast for the fleet, unusable for the person waiting.
    _m(f"{PERF}.request_output_tps", "Single-stream output tokens/s", "higher",
       "Output tokens/s one stream sees, at the concurrency the sweep reported "
       "(output_tps ÷ concurrency). How fast the service FEELS, as opposed to "
       "how much of it there is.", unit="tok/s", headline=True),
    _m(f"{PERF}.c1.request_output_tps", "Single-stream output tokens/s (unloaded)",
       "higher",
       "The same rate measured at concurrency 1 — one request, nothing else "
       "running. The ceiling for a single stream; compare it against the loaded "
       "figure to see what contention costs.", unit="tok/s"),
    _m(f"{PERF}.c1.ttft_p99_ms", "TTFT p99 (unloaded)", "lower",
       "Time to first token with no queueing at all. Isolates the model's own "
       "prefill cost from load effects.", unit="ms"),

    # -- latency -------------------------------------------------------------
    _m(f"{PERF}.ttft_p99_ms", "TTFT p99", "lower",
       "Worst-case time to first token. The usual SLO for interactive traffic.",
       unit="ms", headline=True),
    _m(f"{PERF}.ttft_p90_ms", "TTFT p90", "lower",
       "Time to first token for the slowest tenth. guidellm reports p50/p90/p99 "
       "and the mean — there is no p95.", unit="ms"),
    _m(f"{PERF}.ttft_p50_ms", "TTFT median", "lower", "Typical time to first token.",
       unit="ms"),
    _m(f"{PERF}.itl_p99_ms", "Inter-token latency p99", "lower",
       "Worst-case gap between streamed tokens — how stuttery output feels.",
       unit="ms"),
    _m(f"{PERF}.tpot_p99_ms", "Time per output token p99", "lower",
       "Worst-case per-token decode time.", unit="ms"),

    # -- capacity / correctness ---------------------------------------------
    _m(f"{PERF}.peak_concurrency", "Peak concurrency", "higher",
       "Highest concurrency the sweep sustained within its SLO. If this stays "
       "low, the benchmark — not the config — is the limit."),
    _m(f"{PERF}.uptime", "Uptime", "higher", "Fraction of the run the service answered."),
    _m(f"{PERF}.http_status_200", "Successful responses", "higher",
       "Requests the service answered with a 200. Falling below the request "
       "count means errors under load that throughput alone will not show."),
    _m(f"{FUNC}.pass_rate", "Functional pass rate", "higher",
       "Share of correctness checks passed. Belongs in a constraint: a fast "
       "service that answers wrongly is not a faster service.", headline=True),
    _m("score_total", "LLMBench composite score", "higher",
       "The benchmark's own weighted score. Changes if the benchmark's weights "
       "are edited, so it is a poor basis for cross-night comparison.",
       group=OVERALL_GROUP),

    # -- production replay ---------------------------------------------------
    # A different benchmark answering a different question: real requests, real
    # prompt lengths, real cache reuse. The names share nothing with the sweep
    # above — replay reports one flat set rather than one set per concurrency
    # level — which is why a staged campaign carries an objective per stage.
    _m(f"{REPLAY}.score_card_norm", "Replay value per card", "higher",
       "The benchmark's own value-weighted score, divided by the cards the "
       "config occupies. Ranks tp=2 against tp=4 fairly, where the raw score "
       "would hand the win to whichever config asked for more GPUs. "
       "The platform default for the replay stage.",
       headline=True, group=REPLAY_GROUP),
    _m(f"{REPLAY}.score", "Replay value (absolute)", "higher",
       "The benchmark's weighted sum of uncached-input, cached and output "
       "tokens per minute — the number its own leaderboard ranks on. Absolute, "
       "so a config on more cards wins for that reason alone.",
       headline=True, group=REPLAY_GROUP),
    _m(f"{REPLAY}.total_tpm_card_norm", "Replay total TPM per card", "higher",
       "All tokens/min per card, unweighted. Treats a cached prompt token as "
       "worth the same as an uncached one; the benchmark prices them 10:1.",
       unit="tok/min/GPU", group=REPLAY_GROUP),
    _m(f"{REPLAY}.output_tpm_card_norm", "Replay output TPM per card", "higher",
       "Decode tokens/min per card. On this traffic — long prompts, short "
       "answers — output is a few percent of the value, so ranking on it alone "
       "ignores most of the work.", unit="tok/min/GPU", group=REPLAY_GROUP),
    _m(f"{REPLAY}.input_tpm_card_norm", "Replay input TPM per card", "higher",
       "Prefill tokens/min per card. The dominant cost on replay traffic.",
       unit="tok/min/GPU", group=REPLAY_GROUP),
    _m(f"{REPLAY}.uncached_input_tpm", "Uncached input TPM", "higher",
       "Prompt tokens/min that actually had to be computed. The largest term "
       "in the value score. No per-card form exists — the benchmark does not "
       "normalize this one.", unit="tok/min", group=REPLAY_GROUP),
    _m(f"{REPLAY}.output_tpm", "Replay output TPM", "higher",
       "Decode tokens/min, absolute.", unit="tok/min", group=REPLAY_GROUP),
    _m(f"{REPLAY}.cache_hit_rate", "Prefix cache hit rate", "higher",
       "Share of prompt tokens served from cache. Real traffic reuses prefixes "
       "heavily; a synthetic sweep with random tokens cannot see this at all.",
       group=REPLAY_GROUP),

    _m(f"{REPLAY}.ttft_p99_ms", "Replay TTFT p99", "lower",
       "Worst-case time to first token on real prompts. Much larger than the "
       "sweep's figure because real prompts are 16-64K tokens, not 1K.",
       unit="ms", headline=True, group=REPLAY_GROUP),
    _m(f"{REPLAY}.ttft_p50_ms", "Replay TTFT median", "lower",
       "Typical time to first token on real prompts.", unit="ms",
       group=REPLAY_GROUP),
    _m(f"{REPLAY}.ttft_32k_64k_p90_ms", "TTFT p90, 32-64K prompts", "lower",
       "Time to first token for the long-prompt bucket, where most replay "
       "traffic sits. A single overall TTFT hides which prompt lengths "
       "regressed.", unit="ms", group=REPLAY_GROUP),
    _m(f"{REPLAY}.total_time_p99_ms", "Replay end-to-end p99", "lower",
       "Worst-case time to a complete answer.", unit="ms", group=REPLAY_GROUP),
    _m(f"{REPLAY}.output_tps_p10", "Slowest tenth, output tok/s", "higher",
       "Per-request decode rate for the slowest tenth of requests. The floor "
       "to put a minimum on: a config can win on aggregate throughput while "
       "the unluckiest requests crawl.", unit="tok/s", group=REPLAY_GROUP),

    _m(f"{REPLAY}.uptime", "Replay uptime", "higher",
       "Fraction of replayed requests the service answered.",
       group=REPLAY_GROUP),
    _m(f"{REPLAY}.error_rate", "Replay error rate", "lower",
       "Share of replayed requests that failed.", group=REPLAY_GROUP),
    _m(f"{REPLAY}.unfinished_rate", "Unfinished rate", "lower",
       "Share of requests that never completed within the request timeout.",
       group=REPLAY_GROUP),
    _m(f"{REPLAY}.judge_good_acc_rate", "Judge: answer quality", "higher",
       "Share of sampled answers a judge model rated acceptable. Only reported "
       "when the benchmark has its judge enabled — an objective naming it "
       "against a benchmark without one scores every run None.",
       group=REPLAY_GROUP),
    _m(f"{REPLAY}.judge_halluc_clear_rate", "Judge: clear hallucinations", "lower",
       "Share of sampled answers the judge called plainly hallucinated. Judge "
       "must be enabled on the benchmark.", group=REPLAY_GROUP),
]

DEFAULT_TARGET_METRIC = f"{PERF}.output_tpm_card_norm"
# What the expensive stage ranks on when a campaign does not say. Card
# normalization is the whole reason this is not simply `replay_prod.score`:
# see the entry above.
DEFAULT_VERIFY_TARGET_METRIC = f"{REPLAY}.score_card_norm"


def lookup(key: str) -> dict[str, Any] | None:
    return next((m for m in METRICS if m["key"] == key), None)


def default_direction(key: str) -> str:
    """A latency metric picked in the UI should default to 'minimize'."""
    spec = lookup(key)
    return "minimize" if spec and spec["better"] == "lower" else "maximize"
