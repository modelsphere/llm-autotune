"""Reconstructing an engine's config from its launch command.

The inverse of the adapters, and the one parser the driver's capture, the
parity check and the baseline canary all share — so production's config is read
the same way everywhere it is read.
"""

from app.control.engine_command import (
    PLACEMENT_FLAGS,
    baseline_engine_args,
    parse_engine_args,
)

# A production sglang command as `docker inspect` records it (argv), captured
# as a JSON string the way the driver stores it.
COMMAND = (
    '["python", "-m", "sglang.launch_server", '
    '"--model-path", "/models/qwen", "--served-model-name", "glm-5", '
    '"--tp", "2", "--chunked-prefill-size", "32768", "--page-size", "64", '
    '"--enable-cache-report", "--port", "8050", "--mem-fraction-static", "0.9"]'
)


def test_flags_before_the_launcher_are_dropped():
    """Everything up to launch_server is docker's business, not the engine's."""
    args = parse_engine_args(COMMAND)
    assert "python" not in args and "m" not in args


def test_valued_flags_and_bare_switches_both_parse():
    args = parse_engine_args(COMMAND)
    assert args["tp"] == "2"
    assert args["chunked_prefill_size"] == "32768"
    assert args["enable_cache_report"] is True  # a switch with no value


def test_a_docker_run_line_parses_the_same_as_an_argv_list():
    line = (
        "docker run --gpus all img python -m sglang.launch_server "
        "--tp 4 --enable-dp-attention"
    )
    args = parse_engine_args(line)
    assert args["tp"] == "4"
    assert args["enable_dp_attention"] is True


def test_baseline_config_strips_placement_and_keeps_the_knobs():
    """What a canary carries: the tuning parameters only, in the same vocabulary
    a search config uses — so where the model lives and which socket it took do
    not read as differences from a swept config."""
    config = baseline_engine_args(COMMAND)
    assert config == {
        "tp": "2",
        "chunked_prefill_size": "32768",
        "page_size": "64",
        "enable_cache_report": True,
        "mem_fraction_static": "0.9",
    }
    assert not (set(config) & PLACEMENT_FLAGS)
    assert "served_model_name" not in config and "port" not in config


def test_an_empty_or_missing_command_yields_an_empty_config():
    """A pre-unification capture with nothing stored must not raise — the canary
    still runs, just with a defaults config."""
    assert baseline_engine_args(None) == {}
    assert baseline_engine_args("") == {}
    assert parse_engine_args(42) == {}


def test_sglang_serve_deploy_script_parses_clean():
    """The newer `sglang serve` CLI (no launch_server token) pasted straight
    from a deploy.sh, backslash line-continuations included. The Kimi-K3/B300
    baseline is why: the old boundary search never fired, so docker's own
    flags (--name, --network, --shm-size) leaked in as engine args, bare
    switches took the escaped newline as their value, and an unindented
    continuation line hid --tokenizer-worker-num entirely."""
    command = (
        "docker run -d \\\n"
        " --name sglang-Kimi-K3-p8050 \\\n"
        " --network host \\\n"
        " --shm-size=32g \\\n"
        " registry.example.com/sglang:v0.5.17-trtllm-fix-cap32 \\\n"
        " sglang serve \\\n"
        " --model-path /model \\\n"
        " --port 8050 \\\n"
        " --served-model-name kimi-k3 \\\n"
        " --trust-remote-code \\\n"
        " --tp-size 8 \\\n"
        " --dcp-size 8 \\\n"
        " --kv-cache-dtype fp8_e4m3 \\\n"
        "--tokenizer-worker-num 4 \\\n"
        " --enable-hierarchical-cache"
    )
    config = baseline_engine_args(command)
    assert config == {
        "trust_remote_code": True,
        "tp_size": "8",
        "dcp_size": "8",
        "kv_cache_dtype": "fp8_e4m3",
        "tokenizer_worker_num": "4",
        "enable_hierarchical_cache": True,
    }


# -- whole-deployment parsing -------------------------------------------------

FULL_DOCKER_RUN = """docker run -d \
  --name sglang-qwen3.8-p8050 \
  --network host \
  --gpus all \
  -v /mnt/disk0/models/Qwen3.8-27B-FP8:/model \
  -v /root/deploy/qwen3.8-p8050/.cache:/root/.cache \
  --ipc=host \
  --ulimit memlock=-1 \
  --ulimit stack=67108864 \
  -e NVIDIA_DISABLE_REQUIRE=1 \
  -e PYTORCH_ALLOC_CONF=expandable_segments:True \
  registry.example.com/sglang:v0.5.15-cu129 \
  sglang serve \
  --trust-remote-code \
  --model-path /model \
  --host 0.0.0.0 \
  --port 8050 \
  --served-model-name qwen3.8 \
  --mem-fraction-static 0.85 \
  --mamba-full-memory-ratio 4.59 \
  --enable-cache-report"""


def test_a_whole_docker_run_line_compiles_into_structured_fields():
    from app.control.engine_command import parse_launch_command

    out = parse_launch_command(FULL_DOCKER_RUN)
    assert out["engine"] == "sglang"
    assert out["image"] == "registry.example.com/sglang:v0.5.15-cu129"
    assert out["served_model_name"] == "qwen3.8"
    assert out["service_port"] == 8050
    # The engine saw /model; the host path comes off the -v that put it there,
    # and that mount is dropped (the platform mounts the weights itself).
    assert out["model_path"] == "/mnt/disk0/models/Qwen3.8-27B-FP8"
    assert out["extra_volumes"] == {"/root/deploy/qwen3.8-p8050/.cache": "/root/.cache"}
    assert out["extra_env"] == {
        "NVIDIA_DISABLE_REQUIRE": "1",
        "PYTORCH_ALLOC_CONF": "expandable_segments:True",
    }
    # Tuning knobs only — placement stripped; the unknown mamba flag rides along.
    assert out["engine_args"]["mem_fraction_static"] == "0.85"
    assert out["engine_args"]["mamba_full_memory_ratio"] == "4.59"
    assert out["engine_args"]["enable_cache_report"] is True
    assert "model_path" not in out["engine_args"]
    assert "port" not in out["engine_args"]
    # Platform-set docker flags are recognized and dropped LOUDLY.
    managed = " ".join(out["platform_managed"])
    assert "--gpus all" in managed and "--network host" in managed
    assert "--ipc" in managed and "--name sglang-qwen3.8-p8050" in managed
    assert out["warnings"] == []


def test_a_bare_serve_command_still_yields_the_placement_fields():
    from app.control.engine_command import parse_launch_command

    out = parse_launch_command(
        "python -m sglang.launch_server --model-path /mnt/models/glm --port 8000 "
        "--served-model-name glm-5 --tp 2"
    )
    assert out["image"] == ""
    assert out["model_path"] == "/mnt/models/glm"
    assert out["service_port"] == 8000
    assert out["served_model_name"] == "glm-5"
    assert out["engine_args"] == {"tp": "2"}


def test_what_cannot_transfer_is_warned_never_silent():
    from app.control.engine_command import parse_launch_command

    out = parse_launch_command(
        "docker run --shm-size 16g -e SECRET -p 31000:31000 img "
        "python -m sglang.launch_server --port 31000 --tp 2"
    )
    text = " ".join(out["warnings"])
    assert "--shm-size" in text, "an uncarryable docker flag is named"
    assert "SECRET" in text, "a host-inherited env var is named"
    assert "kube-proxy" in text, "a NodePort-range port is called out"
    assert "-p 31000:31000" in " ".join(out["platform_managed"])


def test_trailing_whitespace_after_a_line_continuation_does_not_eat_the_image():
    # A paste from a wiki carries "\ " — backslash, then a stray space — at the
    # end of each line. shlex reads that as an escaped space and emits a bare
    # " " token, which the docker walk took for the image: the field filled
    # with a space (looking empty), and the -v mounts, -e envs and real image
    # all drifted into the engine scan (found live on the submit dialog).
    from app.control.engine_command import parse_launch_command

    out = parse_launch_command(
        "docker run -d \\ \n"
        "  --name sg \\ \n"
        "  -v /mnt/models/Qwen3.8-27B-FP8:/model \\ \n"
        "  -e NVIDIA_DISABLE_REQUIRE=1 \\ \n"
        "  registry.example.com/sglang:v0.5.15-cu129 \\ \n"
        "  sglang serve \\ \n"
        "  --model-path /model --port 8050 --tp 1"
    )
    assert out["image"] == "registry.example.com/sglang:v0.5.15-cu129"
    assert out["model_path"] == "/mnt/models/Qwen3.8-27B-FP8", "resolved through the -v"
    assert out["extra_env"] == {"NVIDIA_DISABLE_REQUIRE": "1"}
    assert out["warnings"] == []


def test_a_container_model_path_with_no_mount_is_warned():
    # `--model-path /model` names a path INSIDE the container; without a -v in
    # the paste there is nothing to resolve it to a host path — say so instead
    # of quietly storing a path no machine has.
    from app.control.engine_command import parse_launch_command

    out = parse_launch_command(
        "docker run -d img sglang serve --model-path /model --port 8050 --tp 1"
    )
    assert out["model_path"] == "/model"
    assert any("inside the container" in w for w in out["warnings"])


def test_vllm_short_flags_and_aliases_compile_to_the_canonical_keys():
    from app.control.engine_command import parse_launch_command

    out = parse_launch_command(
        "docker run -d img vllm serve /model -tp 2 -q fp8 "
        "--gpu-memory-utilization 0.9 --no-enable-prefix-caching --port 8000"
    )
    assert out["engine"] == "vllm"
    assert out["engine_args"]["tp"] == "2"
    assert out["engine_args"]["quantization"] == "fp8"
    assert out["engine_args"]["enable_prefix_caching"] is False
    assert "gpu_memory_utilization" in out["engine_args"]
    assert out["normalized"], "the folds are reported to the editor"


def test_sglang_long_spellings_fold_into_the_shared_vocabulary():
    from app.control.engine_command import parse_launch_command

    out = parse_launch_command(
        "sglang serve --model-path /m --tensor-parallel-size 4 "
        "--data-parallel-size 2 --no-enable-multimodal --port 8000"
    )
    assert out["engine_args"]["tp"] == "4"
    assert out["engine_args"]["dp"] == "2"
    assert out["engine_args"]["enable_multimodal"] is False
