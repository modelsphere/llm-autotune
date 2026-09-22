"""Regression baseline: campaign 20, "node-24 chunk x backend test 3".

A real, finished, deterministic run on 8×A100 — recorded here so that any
refactor of the space expander, the validator, the engine adapter, the
aggregator or the report can be checked against behaviour that actually
happened rather than against a fixture someone invented.

Captured 2026-08-03 from the live platform database (campaign 20, runs 49-52).
Everything below is verbatim: the search space, the objective, the exact
`docker run` line the driver produced for run 49, and the metrics LLMBench
returned. Nothing here is illustrative.

The experiment: sglang v0.5.10.post1 serving Qwen3.6-35B-A3B on node-24, tp=2,
sweeping attention_backend × chunked_prefill_size. Four candidates, four runs,
all succeeded, packed two cards each across the eight-card node.

What it fixes in place:
  - the space expands to exactly those four configs
  - each is valid, and occupies exactly two GPUs
  - the rendered launch command is byte-identical to the recorded one
  - the objective ranks them in the order the night actually produced
  - all four are feasible (pass_rate 1.0 against a >= 0.99 redline)
  - the report names the same winner

Seeds are not part of this baseline: neither sglang's launch nor the LLMBench
sweep is seeded today, which is why the numbers below are pinned as *ordering
and magnitude*, not as exact equalities. See test_metric_repeatability.
"""

from app.control.engines import get_adapter
from app.control.launch import LaunchSpec, MachineInfo
from app.control.launch.ssh_docker import render_docker_command
from app.control.search import CandidateConfig
from app.control.search.space import candidate_count, expand
from app.control.search.validation import ValidationContext, cards_used, validate_config
from app.evaluation.aggregate import summarize
from app.objective import sort_key
from app.reporting import render_campaign_report

# -- the campaign, as stored --------------------------------------------------

SEARCH_SPACE = {
    "name": "prefill sweep (50k/1.5k)",
    "base": {
        "tp": 2,
        "page_size": 64,
        "context_length": 262144,
        "enable_metrics": True,
        "reasoning_parser": "qwen3",
        "tool_call_parser": "qwen3_coder",
        "trust_remote_code": True,
        "enable_cache_report": True,
        "mem_fraction_static": 0.9,
        "radix_eviction_policy": "lru",
        "mamba_scheduler_strategy": "extra_buffer",
        "stream_response_default_include_usage": True,
    },
    "grid": {
        "attention_backend": ["flashinfer", "triton"],
        "chunked_prefill_size": [4096, 32768],
    },
}

OBJECTIVE = {
    "name": "Throughput per GPU",
    "direction": "maximize",
    "target_metric": "perf_guidellm_sweep.output_tpm_card_norm",
    "redlines": [
        {"metric": "functional_acceptance.pass_rate", "op": ">=", "value": 0.99}
    ],
}

MODEL_PATH = "/mnt/disk0/models/modelforge/Qwen3.6-35B-A3B-793303"
IMAGE = "registry.example.com/sglang:v0.5.10.post1"
SERVED_MODEL = "glm-5"
EXTRA_ENV = {"NCCL_DEBUG": "WARN", "SGLANG_MOE_CONFIG_DIR": "/moe-config"}
EXTRA_VOLUMES = {
    "/root/deploy/modelforge-moe-config-merged": "/moe-config:ro",
    "/root/modelforge-anthropic-compat/protocol.py": (
        "/sgl-workspace/sglang/python/sglang/srt/entrypoints/anthropic/protocol.py:ro"
    ),
}

# The exact command the platform ran for run 49, copied from runs.launch_command
# — with ONE deliberate delta: `--tp 2` is now rendered as `--tp-size 2`, the
# flag's primary spelling (the old form was an argparse abbreviation of the
# same option; the flag-spec catalog renders canonically since 2026-09-01).
RUN_49_LAUNCH_COMMAND = (
    "docker run -d --name autotune-run-49 --network host --gpus '\"device=0,1\"' "
    "--runtime=nvidia --ipc=host --cap-add=SYS_PTRACE --cap-add=SYS_NICE "
    "--ulimit memlock=-1 --ulimit stack=67108864 --security-opt label=disable "
    "--restart no -e NCCL_DEBUG=WARN -e NVIDIA_DISABLE_REQUIRE=1 "
    "-e PYTORCH_ALLOC_CONF=expandable_segments:True -e SGLANG_MOE_CONFIG_DIR=/moe-config "
    f"-v {MODEL_PATH}:/model "
    "-v /root/deploy/autotune/run-49/cache:/root/.cache "
    "-v /root/deploy/modelforge-moe-config-merged:/moe-config:ro "
    "-v /root/modelforge-anthropic-compat/protocol.py:"
    "/sgl-workspace/sglang/python/sglang/srt/entrypoints/anthropic/protocol.py:ro "
    f"--entrypoint python3 {IMAGE} "
    "-m sglang.launch_server --model-path /model --served-model-name glm-5 "
    "--host 0.0.0.0 --port 28200 --attention-backend flashinfer "
    "--chunked-prefill-size 4096 --context-length 262144 --enable-cache-report "
    "--enable-metrics --mamba-scheduler-strategy extra_buffer "
    "--mem-fraction-static 0.9 --page-size 64 --radix-eviction-policy lru "
    "--reasoning-parser qwen3 --stream-response-default-include-usage "
    "--tool-call-parser qwen3_coder --tp-size 2 --trust-remote-code"
)

# Metrics LLMBench returned, per run. Trimmed to the keys the platform reasons
# about; the full set is 101 keys and lives in results.raw on the box.
RUNS = {
    49: {"backend": "flashinfer", "chunk": 4096, "tpm_card": 61868.6, "out_tps": 257.79,
         "req_out_tps": 64.45, "ttft_p99": 11452.0, "pass_rate": 1.0, "single_stream_otps": 129.82},
    50: {"backend": "flashinfer", "chunk": 32768, "tpm_card": 69288.3, "out_tps": 288.70,
         "req_out_tps": 72.18, "ttft_p99": 8541.0, "pass_rate": 1.0, "single_stream_otps": 129.82},
    51: {"backend": "triton", "chunk": 4096, "tpm_card": 34374.7, "out_tps": 143.23,
         "req_out_tps": 35.81, "ttft_p99": 19318.0, "pass_rate": 1.0, "single_stream_otps": 129.82},
    52: {"backend": "triton", "chunk": 32768, "tpm_card": 38604.7, "out_tps": 160.85,
         "req_out_tps": 40.21, "ttft_p99": 14088.0, "pass_rate": 1.0, "single_stream_otps": 129.82},
}


def _metrics(run: dict) -> dict:
    return {
        "perf_guidellm_sweep.output_tpm_card_norm": run["tpm_card"],
        "perf_guidellm_sweep.output_tps": run["out_tps"],
        "perf_guidellm_sweep.request_output_tps": run["req_out_tps"],
        "perf_guidellm_sweep.ttft_p99_ms": run["ttft_p99"],
        "perf_guidellm_sweep.c1.request_output_tps": run["single_stream_otps"],
        "functional_acceptance.pass_rate": run["pass_rate"],
    }


def _config(backend: str, chunk: int) -> dict:
    return {**SEARCH_SPACE["base"], "attention_backend": backend, "chunked_prefill_size": chunk}


# -- 1. the space expands to exactly what ran ---------------------------------


def test_the_space_expands_to_the_four_configurations_that_ran():
    configs = expand(SEARCH_SPACE)
    assert candidate_count(SEARCH_SPACE) == 4
    assert len(configs) == 4
    swept = {(c["attention_backend"], c["chunked_prefill_size"]) for c in configs}
    assert swept == {
        ("flashinfer", 4096), ("flashinfer", 32768),
        ("triton", 4096), ("triton", 32768),
    }
    # The base is carried onto every candidate — losing it is how a campaign
    # silently stops running the configuration it was set up to test.
    assert all(c["tp"] == 2 and c["context_length"] == 262144 for c in configs)


def test_config_hashes_match_the_ones_stored_in_production():
    """Verbatim from candidates.config_hash for campaign 20.

    The hash is how a point is recognized as already-tried, across worker
    restarts and across releases. If it moves, every past campaign's history
    stops matching and the platform re-runs nights it has already done. These
    four were computed by the code that ran on 2026-07-30 and are reproduced
    by the code today — which is the evidence that the refactor did not change
    candidate identity.
    """
    stored = {
        ("flashinfer", 4096): "f8f6f7cad4e54d42",
        ("flashinfer", 32768): "8c581fdcbcf28631",
        ("triton", 4096): "25ca7e55a893b889",
        ("triton", 32768): "2ef1b7877b4975ce",
    }
    for (backend, chunk), expected in stored.items():
        assert CandidateConfig(engine_args=_config(backend, chunk)).hash == expected


# -- 2. validation and placement ----------------------------------------------


def test_all_four_are_valid_on_the_eight_card_node():
    ctx = ValidationContext(gpu_count=8, engine="sglang")
    for config in expand(SEARCH_SPACE):
        assert validate_config(config, ctx) is None


def test_each_config_occupies_two_cards_so_four_fit_at_once():
    """node-24 ran all four simultaneously, pinned to [0,1] [2,3] [4,5] [6,7].
    If cards_used were wrong the night would have taken four rounds."""
    for config in expand(SEARCH_SPACE):
        assert cards_used(config) == 2
    assert sum(cards_used(c) for c in expand(SEARCH_SPACE)) == 8


# -- 3. the launch command ----------------------------------------------------


def test_the_rendered_command_is_byte_identical_to_what_ran():
    """The strongest single regression check available: this exact string
    started a 35B model on real hardware and produced the numbers below."""
    import shlex

    spec = LaunchSpec(
        run_id=49,
        machine=MachineInfo(name="node-24", host="10.0.0.1", gpu_count=8),
        engine="sglang",
        image=IMAGE,
        model_path=MODEL_PATH,
        served_model_name=SERVED_MODEL,
        engine_args=_config("flashinfer", 4096),
        gpu_indices=[0, 1],
        port=28200,
        env=EXTRA_ENV,
        volumes=EXTRA_VOLUMES,
    )
    rendered = " ".join(shlex.quote(part) for part in render_docker_command(spec))
    assert rendered == RUN_49_LAUNCH_COMMAND


def test_the_engine_adapter_produces_the_recorded_server_arguments():
    spec = LaunchSpec(
        run_id=49,
        machine=MachineInfo(name="node-24", host="10.0.0.1", gpu_count=8),
        engine="sglang", image=IMAGE, model_path=MODEL_PATH,
        served_model_name=SERVED_MODEL,
        engine_args=_config("triton", 32768), port=28200,
    )
    command = " ".join(get_adapter("sglang").build_command(spec))
    assert command.startswith("python3 -m sglang.launch_server --model-path /model")
    assert "--attention-backend triton" in command
    assert "--chunked-prefill-size 32768" in command
    # Booleans render as bare switches, never as `--flag True`.
    assert "--trust-remote-code" in command and "--trust-remote-code True" not in command


# -- 4. scoring, feasibility and ranking --------------------------------------


def test_every_run_was_feasible_and_scored_as_recorded():
    for run_id, run in RUNS.items():
        summary = summarize(OBJECTIVE, _metrics(run))
        assert summary.objective_value == run["tpm_card"], run_id
        assert summary.feasible, run_id
        assert not summary.breaches, run_id
        # pass_rate 1.0 against a >= 0.99 floor is 0.01 of slack, i.e. -0.01.
        assert summary.constraints == (0.99 - 1.0,)


def test_the_ranking_reproduces_the_night():
    """flashinfer beat triton by ~80%, and the larger prefill chunk won within
    each backend. Both effects are far outside the 1% noise band."""
    ranked = sorted(
        RUNS.items(),
        key=lambda item: sort_key(
            summarize(OBJECTIVE, _metrics(item[1])).objective_value, OBJECTIVE
        ),
    )
    assert [run_id for run_id, _ in ranked] == [50, 49, 52, 51]

    best, worst = RUNS[50]["tpm_card"], RUNS[51]["tpm_card"]
    assert best / worst > 2.0, "flashinfer @ 32k was more than twice triton @ 4k"


def test_the_report_names_the_winner_the_night_produced():
    runs, results = [], {}
    for run_id, run in RUNS.items():
        runs.append(_ReportRun(run_id, _config(run["backend"], run["chunk"])))
        results[run_id] = _ReportResult(run_id, _metrics(run), OBJECTIVE)

    report = render_campaign_report(_ReportCampaign(), runs, results, {1: _Machine()})

    verdict = report.split("## Results", maxsplit=1)[0]
    assert "attention_backend=flashinfer" in verdict
    assert "chunked_prefill_size=32768" in verdict
    # Only the two swept parameters belong in the table; the twelve-key base
    # made the first real report unreadable.
    assert "context_length" not in report
    assert "No successful runs" not in report


# -- 5. the catalog must describe metrics that actually exist ----------------

# Every metric key campaign 20's benchmark returned, verbatim from
# `select distinct jsonb_object_keys(metrics)`. Nested groups are listed in the
# flattened form the adapter now produces.
REAL_KEYS = {
    "score_total",
    "functional_acceptance.pass_rate",
    "functional_acceptance.score",
    "functional_acceptance.tests_passed",
    "functional_acceptance.tests_total",
    "functional_acceptance.tests_failed",
    "functional_acceptance.tests_skipped",
    "functional_acceptance.tests_executed",
    f"{'perf_guidellm_sweep'}.score",
    "perf_guidellm_sweep.output_tpm_card_norm",
    "perf_guidellm_sweep.total_tpm_card_norm",
    "perf_guidellm_sweep.input_tpm_card_norm",
    "perf_guidellm_sweep.output_tps",
    "perf_guidellm_sweep.input_tps",
    "perf_guidellm_sweep.request_output_tps",
    "perf_guidellm_sweep.output_tps_mean",
    "perf_guidellm_sweep.output_tps_p50",
    "perf_guidellm_sweep.output_tps_p90",
    "perf_guidellm_sweep.output_tps_p99",
    "perf_guidellm_sweep.total_tps_mean",
    "perf_guidellm_sweep.total_tps_p50",
    "perf_guidellm_sweep.total_tps_p90",
    "perf_guidellm_sweep.total_tps_p99",
    "perf_guidellm_sweep.input_tps_mean",
    "perf_guidellm_sweep.input_tps_p50",
    "perf_guidellm_sweep.input_tps_p90",
    "perf_guidellm_sweep.input_tps_p99",
    "perf_guidellm_sweep.ttft_mean_ms",
    "perf_guidellm_sweep.ttft_p50_ms",
    "perf_guidellm_sweep.ttft_p90_ms",
    "perf_guidellm_sweep.ttft_p99_ms",
    "perf_guidellm_sweep.itl_mean_ms",
    "perf_guidellm_sweep.itl_p50_ms",
    "perf_guidellm_sweep.itl_p90_ms",
    "perf_guidellm_sweep.itl_p99_ms",
    "perf_guidellm_sweep.tpot_mean_ms",
    "perf_guidellm_sweep.tpot_p50_ms",
    "perf_guidellm_sweep.tpot_p90_ms",
    "perf_guidellm_sweep.tpot_p99_ms",
    "perf_guidellm_sweep.peak_concurrency",
    "perf_guidellm_sweep.peak_output_tps",
    "perf_guidellm_sweep.peak_input_tps",
    "perf_guidellm_sweep.peak_total_tps",
    "perf_guidellm_sweep.reported_concurrency",
    "perf_guidellm_sweep.reported_level_meets_slo",
    "perf_guidellm_sweep.http_status_200",
    "perf_guidellm_sweep.uptime",
    # per-concurrency groups, flattened
    *(
        f"perf_guidellm_sweep.c{level}.{metric}"
        for level in (1, 2, 4)
        for metric in (
            "request_output_tps", "ttft_p50_ms", "ttft_p90_ms", "ttft_p99_ms",
            "ttft_mean_ms", "itl_p50_ms", "itl_p90_ms", "itl_p99_ms", "itl_mean_ms",
            "tpot_p50_ms", "tpot_p90_ms", "tpot_p99_ms", "tpot_mean_ms",
            "output_tps_p50", "output_tps_p90", "output_tps_p99", "output_tps_mean",
            "input_tps_p50", "input_tps_p90", "input_tps_p99", "input_tps_mean",
            "total_tps_p50", "total_tps_p90", "total_tps_p99", "total_tps_mean",
            "duration_seconds", "measured_requests", "meets_slo", "uptime",
        )
    ),
}


def test_every_offered_sweep_metric_exists_in_a_real_benchmark_result():
    """A catalog entry is a promise that an objective built on it will work.

    It offered `perf_guidellm_sweep.total_tps`, which guidellm has never
    returned — it reports that as a distribution. A campaign targeting it
    would have scored every run None, marked every one infeasible, and
    reported "no config stayed inside the redlines" after a full night.

    Scoped to the metrics THIS submission returned. The replay benchmark shares
    none of these names and is pinned against its own real submission in
    test_regression_submission152 — checking both here would mean asserting
    that one benchmark returns another's keys.
    """
    from app.metrics_catalog import FUNC, METRICS, PERF

    offered = {
        m["key"]
        for m in METRICS
        if m["key"].startswith((PERF, FUNC)) or m["key"] == "score_total"
    }
    assert offered <= REAL_KEYS, f"offered but never returned: {sorted(offered - REAL_KEYS)}"


def test_the_catalog_offers_nothing_from_a_benchmark_nobody_pinned():
    """Every offered key belongs to a module some regression file has a real
    submission for. Without this, a new entry can be added under a brand-new
    module prefix and slip past both pins by matching neither."""
    from app.metrics_catalog import METRICS
    from tests.test_regression_submission152 import REAL_KEYS as REPLAY_KEYS

    offered = {m["key"] for m in METRICS}
    assert offered <= REAL_KEYS | REPLAY_KEYS, (
        f"offered by no pinned benchmark: {sorted(offered - REAL_KEYS - REPLAY_KEYS)}"
    )


def test_the_single_stream_constraint_from_the_brief_is_expressible():
    """"minimum single-stream OTPS" needs a scalar key to point at. The number
    was always in the payload, nested one level down in the concurrency-1
    group, and therefore unusable in an objective until it was flattened."""
    objective = {
        "target_metric": "perf_guidellm_sweep.output_tpm_card_norm",
        "redlines": [
            {"metric": "perf_guidellm_sweep.c1.request_output_tps", "op": ">=", "value": 100.0},
            {"metric": "perf_guidellm_sweep.ttft_p99_ms", "op": "<=", "value": 12000.0},
        ],
    }
    summary = summarize(objective, _metrics(RUNS[50]))
    assert summary.feasible, "129.8 tok/s single-stream and 8541ms TTFT both hold"
    # 129.82 against a 100 floor is 29.82 of slack, i.e. -29.82.
    assert summary.constraints[0] == 100.0 - 129.82

    tighter = {**objective, "redlines": [
        {"metric": "perf_guidellm_sweep.c1.request_output_tps", "op": ">=", "value": 200.0},
    ]}
    breached = summarize(tighter, _metrics(RUNS[50]))
    assert not breached.feasible
    assert breached.constraints[0] > 0, "crossed reads positive"


# -- report doubles -----------------------------------------------------------


class _ReportRun:
    def __init__(self, run_id: int, config: dict):
        self.id = run_id
        self.kind = "experiment"
        self.status = "succeeded"
        self.failure_class = ""
        self.error = ""
        self.machine_id = 1
        self.candidate = type("C", (), {"config": config})()


class _ReportResult:
    def __init__(self, run_id: int, metrics: dict, objective: dict):
        summary = summarize(objective, metrics)
        self.run_id = run_id
        self.metrics = metrics
        self.objective_value = summary.objective_value
        self.feasible = summary.feasible
        self.constraints = list(summary.constraints)
        self.breaches = summary.breaches


class _ReportCampaign:
    name = "node-24 chunk x backend test 3"
    objective = OBJECTIVE
    model_path = MODEL_PATH
    engine = "sglang"
    image = IMAGE
    search_space = SEARCH_SPACE


class _Machine:
    name = "node-24"
