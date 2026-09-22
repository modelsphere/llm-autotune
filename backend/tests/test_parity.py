"""Comparing a campaign's config against the production command we captured.

Campaign 19 lost a night because its base config omitted --enable-cache-report,
which production runs with: every candidate failed the benchmark's cache check
for a reason unrelated to the parameters being swept.
"""

from app.control.search.parity import missing_flags, parse_engine_args

# Exactly what capture recorded for node-24's sglang-modelforge-0.2-p8050.
PRODUCTION = {
    "services": [
        {
            "container": "sglang-modelforge-0.2-p8050",
            "served_model_name": "glm-5",
            "command": [
                "-m", "sglang.launch_server",
                "--model-path", "/model",
                "--host", "0.0.0.0",
                "--port", "8050",
                "--served-model-name", "glm-5",
                "--tp", "2",
                "--context-length", "262144",
                "--mem-fraction-static", "0.90",
                "--page-size", "64",
                "--chunked-prefill-size", "16384",
                "--trust-remote-code",
                "--enable-cache-report",
                "--enable-metrics",
                "--stream-response-default-include-usage",
                "--reasoning-parser", "qwen3",
            ],
        }
    ]
}

CAMPAIGN_19 = {
    "tp": 2,
    "page_size": 64,
    "context_length": 262144,
    "reasoning_parser": "qwen3",
    "trust_remote_code": True,
    "mem_fraction_static": 0.9,
    "chunked_prefill_size": 32768,
}


def test_bare_switches_and_valued_flags_both_parse():
    args = parse_engine_args(PRODUCTION["services"][0]["command"])
    assert args["enable_cache_report"] is True
    assert args["chunked_prefill_size"] == "16384"
    assert "launch_server" not in args


def test_a_docker_run_line_parses_the_same_way():
    """Capture has stored argv lists and whole `docker run` lines over time."""
    line = (
        "docker run -d --name x --gpus all img -m sglang.launch_server "
        "--model-path /model --tp 2 --enable-cache-report"
    )
    args = parse_engine_args(line)
    assert args == {"model_path": "/model", "tp": "2", "enable_cache_report": True}


def test_the_flag_that_cost_campaign_19_a_night_is_reported():
    missing = {m["flag"] for m in missing_flags(CAMPAIGN_19, PRODUCTION, "glm-5")}
    assert "enable_cache_report" in missing
    assert "enable_metrics" in missing
    assert "stream_response_default_include_usage" in missing


def test_placement_flags_are_not_differences():
    """Where the model lives and which socket it binds are set per run."""
    missing = {m["flag"] for m in missing_flags(CAMPAIGN_19, PRODUCTION, "glm-5")}
    assert not missing & {"model_path", "host", "port", "served_model_name"}


def test_a_different_value_is_the_point_of_the_campaign_not_a_warning():
    """Production runs chunked_prefill_size 16384; the campaign sweeps 32768.
    That is the experiment, not drift."""
    missing = {m["flag"] for m in missing_flags(CAMPAIGN_19, PRODUCTION, "glm-5")}
    assert "chunked_prefill_size" not in missing
    assert "mem_fraction_static" not in missing


def test_no_capture_means_nothing_to_compare():
    assert missing_flags(CAMPAIGN_19, None) == []
    assert missing_flags(CAMPAIGN_19, {"services": []}) == []
