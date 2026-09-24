from app.control.launch import LaunchSpec, MachineInfo
from app.control.launch.ssh_docker import (
    classify_failure,
    render_docker_command,
    render_engine_command,
)


def _spec(engine: str = "sglang", **kwargs) -> LaunchSpec:
    return LaunchSpec(
        run_id=42,
        machine=MachineInfo(name="gpu-01", host="10.0.0.1", gpu_count=8),
        engine=engine,
        image="lmsysorg/sglang:latest",
        model_path="/mnt/disk0/models/qwen3-32b",
        served_model_name="qwen3-32b",
        engine_args={"tp_size": 4, "mem_fraction_static": 0.9, "enable_torch_compile": True},
        **kwargs,
    )


def test_sglang_command_uses_model_mount():
    cmd = render_engine_command(_spec())
    text = " ".join(cmd)
    assert cmd[0] == "python3"
    assert "sglang.launch_server" in text
    assert "--model-path /model" in text  # host path is bind-mounted, never used directly
    assert "--tp-size 4" in text
    assert "--mem-fraction-static 0.9" in text
    assert "--enable-torch-compile" in text  # bool → bare flag
    assert "--enable-torch-compile True" not in text


def test_vllm_command_rendering():
    cmd = render_engine_command(_spec(engine="vllm"))
    assert cmd[:3] == ["vllm", "serve", "/model"]


def test_a_declared_volume_beats_the_default_on_the_same_container_path():
    # A prewarmed cache mounted at /root/.cache must REPLACE the platform's
    # per-run cache mount, not sit beside it — docker refuses "Duplicate mount
    # point", which is a launch failure the user cannot see coming.
    spec = _spec(volumes={"/root/deploy/qwen/.cache": "/root/.cache"})
    text = " ".join(render_docker_command(spec))
    assert "-v /root/deploy/qwen/.cache:/root/.cache" in text
    assert text.count(":/root/.cache") == 1, "one mount per container path"

    # Same rule for /model — and an :ro suffix still counts as the same target.
    spec = _spec(volumes={"/mnt/other/weights": "/model:ro"})
    text = " ".join(render_docker_command(spec))
    assert "-v /mnt/other/weights:/model:ro" in text
    assert "-v /mnt/disk0/models/qwen3-32b:/model" not in text


def test_docker_command_matches_prod_conventions():
    spec = _spec(gpu_indices=[0, 1, 2, 3])
    cmd = render_docker_command(spec)
    text = " ".join(cmd)
    assert "--name autotune-run-42" in text
    assert "device=0,1,2,3" in text
    assert "--ipc=host" in text
    assert "--runtime=nvidia" in text
    assert "memlock=-1" in text
    assert "-e NVIDIA_DISABLE_REQUIRE=1" in text
    assert "-e PYTORCH_ALLOC_CONF=expandable_segments:True" in text
    assert "-v /mnt/disk0/models/qwen3-32b:/model" in text
    assert "-v /root/deploy/autotune/run-42/cache:/root/.cache" in text
    # engine binary becomes the entrypoint; args follow the image
    assert "--entrypoint python3" in text
    assert cmd[cmd.index(spec.image) + 1 :][0] == "-m"
    # default port stays out of the k8s NodePort range (30000-32767)
    assert spec.endpoint_url == "http://10.0.0.1:28200"
    assert not 30000 <= spec.port <= 32767


def test_env_overrides_defaults():
    spec = _spec(env={"PYTORCH_ALLOC_CONF": "max_split_size_mb:512"})
    text = " ".join(render_docker_command(spec))
    assert "-e PYTORCH_ALLOC_CONF=max_split_size_mb:512" in text
    assert "expandable_segments" not in text


def test_failure_classification():
    assert classify_failure("torch.cuda.OutOfMemoryError: CUDA out of memory") == "oom"
    assert classify_failure("NCCL error: unhandled system error") == "nccl"
    assert classify_failure("error: argument --tp-size: invalid int value") == "bad_config"
    assert classify_failure("port 30000 already in use on 10.0.0.1: LISTEN") == "port_conflict"
    assert (
        classify_failure("Unable to find image 'lmsysorg/sglang:latest' locally")
        == "image_missing"
    )
    assert classify_failure("something exotic") == "unknown"
