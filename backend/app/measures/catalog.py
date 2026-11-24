"""The measures: one vocabulary for what a benchmark found, whichever module found it.

LLMBench names a number the way the module that produced it happens to: a
guidellm sweep's `output_tps_mean` is the aggregate output rate of a whole
level, a replay's `output_tps_mean` is the speed ONE request saw, and
opencompass's `output_tps` is the second of those under the first one's name.
`uptime` is "1 − errored/total" in one module and "successful/attempted" in
another. A rate called `*_card_norm` is per eight cards, not per card. Anyone
writing an objective, a redline or a policy had to know all of that.

A measure is the platform's name for one idea, with a unit, a direction and a
meaning that hold for every module that can report it. The adapters
(`app.measures.adapters`) translate each module's raw keys into measures; the
raw keys stay stored and addressable, for audit and for anything this
vocabulary does not cover.

Some measures take a statistic (`ttft_ms@p99`): which ones a module reports is
part of the module's adapter, never assumed. A statistic a module does not
report is absent — never interpolated from its neighbours, never 0.

Some take a qualifier: the prompt-length bucket a replay splits TTFT by
(`ttft_ms@p90[32k_64k]`), or the task an accuracy suite scored
(`accuracy[gpqa_diamond]`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Better = Literal["higher", "lower"]

THROUGHPUT = "throughput"
REQUEST = "per request"
LATENCY = "latency"
HEALTH = "health"
WORKLOAD = "workload"
QUALITY = "quality"
SCORE = "score"

# The statistics any module reports, in the order a reader expects them.
STATS = ("mean", "p10", "p50", "p90", "p99")

# Replay's prompt-length buckets for TTFT, smallest first (LLMBench's
# TTFT_INPUT_BUCKETS labels).
PROMPT_BUCKETS = ("lt_6k", "6k_16k", "16k_32k", "32k_64k", "64k_128k", "128k_256k", "ge_256k")


@dataclass(frozen=True)
class Measure:
    name: str
    label: str
    unit: str
    # None for a count or a fact: neither more nor fewer is an improvement.
    better: Better | None
    group: str
    meaning: str
    # Statistics it can be asked for. Empty: a single value, no `@stat`.
    stats: tuple[str, ...] = ()
    # The statistic a bare reference means, when there is an obvious one (a
    # per-request speed is a mean unless asked otherwise). None: a reference
    # must name one — "TTFT" alone is not a number.
    default_stat: str | None = None
    # What a qualifier selects (`prompt bucket`, `task`); "" when it takes none.
    qualifier: str = ""
    qualifier_values: tuple[str, ...] = field(default_factory=tuple)

    @property
    def rankable(self) -> bool:
        return self.better is not None


def _m(name, label, unit, better, group, meaning, **kw) -> Measure:
    return Measure(name, label, unit, better, group, meaning, **kw)


_LAT_STATS = ("mean", "p50", "p90", "p99")

MEASURES: tuple[Measure, ...] = (
    # -- throughput: what the deployment delivered ----------------------------
    # Per GPU: divided by the cards the config occupied (tp×dp×pp), so tp=2 and
    # tp=4 are compared on what one card buys. LLMBench's `*_card_norm` is the
    # same rate per EIGHT cards; these are one eighth of it.
    _m(
        "output_tpm_per_gpu",
        "Output tokens/min per GPU",
        "tok/min/GPU",
        "higher",
        THROUGHPUT,
        "Generated tokens per minute of wall clock, divided by the GPUs the config occupies.",
    ),
    _m(
        "input_tpm_per_gpu",
        "Input tokens/min per GPU",
        "tok/min/GPU",
        "higher",
        THROUGHPUT,
        "Prompt tokens processed per minute, cached ones included, per GPU.",
    ),
    _m(
        "uncached_input_tpm_per_gpu",
        "Uncached input tokens/min per GPU",
        "tok/min/GPU",
        "higher",
        THROUGHPUT,
        "Prompt tokens actually computed (not served from the prefix cache) per minute, per GPU.",
    ),
    _m(
        "cached_input_tpm_per_gpu",
        "Cached input tokens/min per GPU",
        "tok/min/GPU",
        "higher",
        THROUGHPUT,
        "Prompt tokens served from the prefix cache per minute, per GPU.",
    ),
    _m(
        "total_tpm_per_gpu",
        "Total tokens/min per GPU",
        "tok/min/GPU",
        "higher",
        THROUGHPUT,
        "Input plus output tokens per minute, per GPU.",
    ),
    _m(
        "output_tpm",
        "Output tokens/min",
        "tok/min",
        "higher",
        THROUGHPUT,
        "Generated tokens per minute for the whole deployment. Rewards using more GPUs; "
        "prefer the per-GPU form unless sizing a fixed deployment.",
    ),
    _m(
        "input_tpm",
        "Input tokens/min",
        "tok/min",
        "higher",
        THROUGHPUT,
        "Prompt tokens per minute for the whole deployment, cached ones included.",
    ),
    _m(
        "uncached_input_tpm",
        "Uncached input tokens/min",
        "tok/min",
        "higher",
        THROUGHPUT,
        "Prompt tokens actually computed per minute, whole deployment.",
    ),
    _m(
        "cached_input_tpm",
        "Cached input tokens/min",
        "tok/min",
        "higher",
        THROUGHPUT,
        "Prompt tokens served from the prefix cache per minute, whole deployment.",
    ),
    _m(
        "total_tpm",
        "Total tokens/min",
        "tok/min",
        "higher",
        THROUGHPUT,
        "Input plus output tokens per minute, whole deployment.",
    ),
    _m(
        "request_rate",
        "Completed requests/s",
        "req/s",
        "higher",
        THROUGHPUT,
        "Requests completed per second of the measurement window, whole deployment.",
    ),
    # -- what one request experienced -----------------------------------------
    _m(
        "request_output_tps",
        "Per-request output speed",
        "tok/s",
        "higher",
        REQUEST,
        "Output tokens per second one request sees, over its whole life (queueing and "
        "prefill included). A sweep reports it as aggregate output ÷ concurrency, a mean "
        "only; a replay measures it per request and also reports its spread.",
        stats=("mean", "p10", "p50", "p90"),
        default_stat="mean",
    ),
    _m(
        "request_prefill_tps",
        "Per-request prefill speed",
        "tok/s",
        "higher",
        REQUEST,
        "Prompt tokens per second of time to first token, per request.",
        stats=("mean", "p10", "p50", "p90"),
        default_stat="mean",
    ),
    _m(
        "ttft_ms",
        "Time to first token",
        "ms",
        "lower",
        LATENCY,
        "From sending a request to its first generated token.",
        stats=_LAT_STATS,
        qualifier="prompt bucket",
        qualifier_values=PROMPT_BUCKETS,
    ),
    _m(
        "itl_ms",
        "Inter-token latency",
        "ms",
        "lower",
        LATENCY,
        "Gap between consecutive streamed tokens.",
        stats=_LAT_STATS,
    ),
    _m(
        "tpot_ms",
        "Time per output token",
        "ms",
        "lower",
        LATENCY,
        "Decode time per generated token after the first.",
        stats=_LAT_STATS,
    ),
    _m(
        "e2e_latency_ms",
        "End-to-end latency",
        "ms",
        "lower",
        LATENCY,
        "From sending a request to its last token.",
        stats=_LAT_STATS,
    ),
    # -- did the service serve ------------------------------------------------
    _m(
        "success_rate",
        "Success rate",
        "",
        "higher",
        HEALTH,
        "Share of requests the service actually received that succeeded (LLMBench calls "
        "it uptime). Requests a benchmark never dispatched are not counted against it.",
    ),
    _m(
        "error_rate",
        "Error rate",
        "",
        "lower",
        HEALTH,
        "Share of requests the service received that failed.",
    ),
    _m(
        "unfinished_rate",
        "Unfinished rate",
        "",
        "lower",
        HEALTH,
        "Share of requests that returned without a finish marker (the stream just stopped).",
    ),
    _m(
        "truncated_rate",
        "Truncated rate",
        "",
        "lower",
        HEALTH,
        "Share of completed requests cut off by the length limit.",
    ),
    _m(
        "not_started_rate",
        "Not-started rate",
        "",
        "lower",
        HEALTH,
        "Share of the workload the benchmark never dispatched because its time ran out. "
        "Above zero, the throughput describes only part of the workload.",
    ),
    _m(
        "meets_slo",
        "Meets the SLO",
        "",
        "higher",
        HEALTH,
        "1 when the operating point held the benchmark's own service target (a sweep's "
        "per-request speed floor and TTFT cap), else 0.",
    ),
    _m(
        "requests_total",
        "Requests in the workload",
        "req",
        None,
        HEALTH,
        "Requests the benchmark meant to send (a sweep: those measured in the window).",
    ),
    _m("requests_completed", "Requests completed", "req", None, HEALTH, "Requests that succeeded."),
    _m("requests_failed", "Requests failed", "req", None, HEALTH, "Requests that errored."),
    _m(
        "requests_not_started",
        "Requests not started",
        "req",
        None,
        HEALTH,
        "Requests never dispatched.",
    ),
    _m(
        "requests_unfinished",
        "Requests unfinished",
        "req",
        None,
        HEALTH,
        "Requests that returned without a finish marker.",
    ),
    _m(
        "requests_truncated",
        "Requests truncated",
        "req",
        None,
        HEALTH,
        "Requests cut off by the length limit.",
    ),
    _m(
        "requests_runaway",
        "Runaway generations",
        "req",
        None,
        HEALTH,
        "Requests the client had to stop because the server generated past the length it "
        "was given, or that ended repeating one token.",
    ),
    # -- what the load was ----------------------------------------------------
    _m(
        "concurrency",
        "Concurrency",
        "",
        None,
        WORKLOAD,
        "Requests in flight at the operating point.",
    ),
    _m(
        "prompt_tokens_mean",
        "Prompt length",
        "tok",
        None,
        WORKLOAD,
        "Mean prompt tokens per request (a sweep: the configured length).",
    ),
    _m(
        "completion_tokens_mean",
        "Completion length",
        "tok",
        None,
        WORKLOAD,
        "Mean generated tokens per request (a sweep: the configured length).",
    ),
    _m(
        "cache_hit_rate",
        "Prefix cache hit rate",
        "",
        "higher",
        WORKLOAD,
        "Share of prompt tokens served from the prefix cache, token-weighted.",
    ),
    _m(
        "window_s",
        "Measurement window",
        "s",
        None,
        WORKLOAD,
        "Length of the window the numbers describe.",
    ),
    _m(
        "prompt_bucket_requests",
        "Requests in a prompt bucket",
        "req",
        None,
        WORKLOAD,
        "Completed requests whose prompt fell in the bucket — read it before trusting the "
        "bucket's TTFT.",
        qualifier="prompt bucket",
        qualifier_values=PROMPT_BUCKETS,
    ),
    # -- did it answer well ---------------------------------------------------
    _m(
        "pass_rate",
        "Functional pass rate",
        "",
        "higher",
        QUALITY,
        "Share of functional checks passed.",
    ),
    _m(
        "tool_call_success_rate",
        "Tool-call success rate",
        "",
        "higher",
        QUALITY,
        "Share of tool-call requests answered with a valid tool call.",
    ),
    _m(
        "hallucination_rate",
        "Hallucination rate",
        "",
        "lower",
        QUALITY,
        "Share of judged answers with a clear hallucination.",
    ),
    _m(
        "answer_quality_rate",
        "Answer quality",
        "",
        "higher",
        QUALITY,
        "Share of judged answers rated good or acceptable.",
    ),
    _m(
        "poor_answer_rate",
        "Poor answers",
        "",
        "lower",
        QUALITY,
        "Share of judged answers rated poor (off-topic, empty, broken format).",
    ),
    _m(
        "long_output_completion_rate",
        "Long-output completion",
        "",
        "higher",
        QUALITY,
        "How much of a requested long output the model produced before stopping.",
    ),
    _m(
        "accuracy",
        "Accuracy",
        "",
        "higher",
        QUALITY,
        "Share of a task's questions answered correctly.",
        qualifier="task",
    ),
    _m("passed_cases_per_hour", "Passed BFCL cases/hour", "cases/h", "higher", THROUGHPUT,
       "Officially passed cases per hour of generation time, excluding grading."),
    _m("case_count", "BFCL cases", "cases", None, WORKLOAD, "Number of evaluated BFCL cases."),
    *(_m(key, label, "", "higher", WORKLOAD, meaning) for key, label, meaning in (
        ("ttft_coverage", "TTFT coverage", "Share of requests with a measured first token."),
        ("input_usage_coverage", "Input usage coverage", "Share with input-token usage."),
        ("output_usage_coverage", "Output usage coverage", "Share with output-token usage."),
        ("cache_usage_coverage", "Cache usage coverage", "Share with cache-token usage."),
    )),
    *(_m(key, label, "", None, WORKLOAD, meaning) for key, label, meaning in (
        ("active_cases_peak", "Peak active cases", "Peak concurrent BFCL cases."),
        ("active_http_peak", "Peak HTTP calls", "Peak concurrent BFCL HTTP attempts."),
        ("http_attempt_count", "HTTP attempts", "HTTP attempts, including retries."),
        ("request_error_count", "Request errors", "Failed HTTP attempts."),
        ("retry_count", "Retries", "Retried HTTP attempts."),
        ("infra_error_count", "Infrastructure errors", "BFCL infrastructure errors."),
    )),
    # -- the benchmark's own verdict ------------------------------------------
    _m(
        "score",
        "Module score",
        "",
        "higher",
        SCORE,
        "The module's own score under the benchmark's weights. For a replay it is a "
        "value-weighted token rate; for a quality suite a weighted accuracy. Changes when "
        "the benchmark's weights are edited.",
    ),
    _m(
        "score_per_gpu",
        "Module score per GPU",
        "",
        "higher",
        SCORE,
        "The module score divided by the GPUs the config occupies — only for a module whose "
        "score is a throughput (a replay).",
    ),
)

BY_NAME: dict[str, Measure] = {m.name: m for m in MEASURES}

# Names LLMBench users already say, accepted in references and resolved to the
# measure they mean.
ALIASES: dict[str, str] = {"uptime": "success_rate"}


def lookup(name: str) -> Measure | None:
    return BY_NAME.get(ALIASES.get(name, name))


def catalogue() -> list[dict]:
    """The catalogue as data, for an editor, a policy or the docs."""
    return [
        {
            "name": m.name,
            "label": m.label,
            "unit": m.unit,
            "better": m.better,
            "group": m.group,
            "meaning": m.meaning,
            "stats": list(m.stats),
            "default_stat": m.default_stat,
            "qualifier": m.qualifier,
            "qualifier_values": list(m.qualifier_values),
        }
        for m in MEASURES
    ]


# What an editor lists first: the numbers most objectives are built from.
_HEADLINE = {
    "output_tpm_per_gpu", "total_tpm_per_gpu", "ttft_ms@p99", "request_output_tps",
    "success_rate", "score_per_gpu",
}


def options() -> list[dict]:
    """Every reference an objective editor can offer, in the shape it lists
    raw keys in (`key`, `label`, `better`, `help`, `unit`, `headline`,
    `group`): one per measure and statistic. A measure that needs a qualifier
    (a task, a prompt bucket) or a scenario prefix is typed, not listed."""
    out = []
    for m in MEASURES:
        if m.qualifier and not m.qualifier_values:
            continue  # accuracy[<task>]: the task is the benchmark's to name
        for stat in m.stats or (None,):
            key = m.name if stat in (None, m.default_stat) else f"{m.name}@{stat}"
            label = m.label if stat in (None, m.default_stat) else f"{m.label} {stat}"
            out.append({
                "key": key,
                "label": label,
                "better": m.better,
                "help": m.meaning,
                "unit": m.unit,
                "headline": key in _HEADLINE,
                "group": m.group.capitalize(),
                "measure": True,
            })
    return out
