"""Capture records enough to rebuild a container.

A deploy script can be stale (node-24's declared --tp 2 while the container ran
tp=4) or absent entirely for a hand-started service — in which case the old
capture left that service with no restore path at all.
"""

import json

from app.control.launch.ssh_docker import _inspect_to_spec, _render_restore_command

INSPECT = json.dumps([
    {
        "Config": {
            "Image": "registry.example.com/sglang:v0.5.10",
            "Cmd": ["-m", "sglang.launch_server", "--port", "8050", "--tp", "4"],
            "Entrypoint": ["python3"],
            "Env": [
                "PATH=/usr/bin",           # docker default — noise
                "SGLANG_MOE_CONFIG_DIR=/moe-config",
                "NCCL_DEBUG=WARN",
            ],
        },
        "HostConfig": {
            "Binds": ["/mnt/disk0/models/m:/model", "/root/cache:/root/.cache:ro"],
            "NetworkMode": "host",
            "IpcMode": "host",
            "Runtime": "nvidia",
            "CapAdd": ["SYS_PTRACE", "SYS_NICE"],
            "SecurityOpt": ["label=disable"],
            "Ulimits": [{"Name": "memlock", "Soft": -1, "Hard": -1}],
            "DeviceRequests": [{"Count": 0, "DeviceIDs": ["0", "1", "2", "3"]}],
            "RestartPolicy": {"Name": "no"},
        },
    }
])


def test_spec_captures_what_matters_for_a_rebuild():
    spec = _inspect_to_spec(INSPECT)
    assert spec["image"] == "registry.example.com/sglang:v0.5.10"
    assert spec["entrypoint"] == ["python3"]
    assert spec["gpu_ids"] == ["0", "1", "2", "3"]
    assert "/mnt/disk0/models/m:/model" in spec["binds"]
    assert "SGLANG_MOE_CONFIG_DIR=/moe-config" in spec["env"]
    assert not any(e.startswith("PATH=") for e in spec["env"]), "drop docker defaults"


def test_rendered_command_reproduces_the_container():
    command = _render_restore_command("sglang-prod-p8050", _inspect_to_spec(INSPECT))
    for fragment in [
        "docker run -d --name sglang-prod-p8050",
        "--network host",
        "--ipc host",
        "--runtime nvidia",
        'device=0,1,2,3',
        "--cap-add SYS_PTRACE",
        "--security-opt label=disable",
        "--ulimit memlock=-1",
        "-e SGLANG_MOE_CONFIG_DIR=/moe-config",
        "-v /mnt/disk0/models/m:/model",
        "--entrypoint python3",
        "registry.example.com/sglang:v0.5.10",
        "-m sglang.launch_server --port 8050 --tp 4",
    ]:
        assert fragment in command, f"missing: {fragment}\ngot: {command}"


def test_unparseable_inspect_degrades_quietly():
    assert _inspect_to_spec("not json") == {}
    assert _render_restore_command("x", {}) == ""
