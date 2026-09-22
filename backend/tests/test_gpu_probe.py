"""The launch-time GPU backstop.

The scheduler's model of "free cards" is its own run ledger; a card a dying
container still pins is invisible to it. `gpus_in_use` reads the hardware
directly so a launch refuses a physically busy card instead of crashing three
minutes into a model load — and the refusal is classified as infrastructure, so
the config is retried elsewhere rather than blamed.
"""

import subprocess

import pytest

from app.control.launch import LaunchSpec, MachineInfo
from app.control.launch.failures import classify_failure, is_infrastructure
from app.control.launch.ssh_docker import SshDockerDriver


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def _spec(**kwargs) -> LaunchSpec:
    return LaunchSpec(
        run_id=7,
        machine=MachineInfo(name="gpu-01", host="10.0.0.1", gpu_count=4),
        engine="sglang",
        image="img",
        model_path="/m",
        served_model_name="m",
        **kwargs,
    )


def test_only_busy_requested_cards_are_flagged(monkeypatch):
    drv = SshDockerDriver()
    # index, memory.used(MiB): card 0 loaded but not asked for, card 2 busy,
    # cards 1 and 3 idle.
    out = "0, 40000\n1, 12\n2, 2048\n3, 0\n"
    monkeypatch.setattr(drv, "_ssh", lambda *a, **k: _completed(out))
    machine = MachineInfo(name="m", host="h", gpu_count=4)
    assert drv.gpus_in_use(machine, [1, 2, 3]) == [2]


def test_no_requested_cards_probes_nothing():
    drv = SshDockerDriver()
    # No ssh call needed, so no monkeypatch — an empty request is free.
    assert drv.gpus_in_use(MachineInfo(name="m", host="h"), []) == []


def test_a_probe_that_cannot_read_trusts_the_ledger(monkeypatch):
    drv = SshDockerDriver()
    monkeypatch.setattr(drv, "_ssh", lambda *a, **k: _completed("", returncode=1))
    # No nvidia-smi / an ssh blip must never be a new way for a launch to fail.
    assert drv.gpus_in_use(MachineInfo(name="m", host="h"), [0, 1]) == []


def test_launch_refuses_a_physically_busy_card(monkeypatch):
    drv = SshDockerDriver()
    monkeypatch.setattr(drv, "_check_port_free", lambda *a, **k: None)
    monkeypatch.setattr(drv, "attach", lambda spec: None)  # no prior container of this run
    monkeypatch.setattr(drv, "gpus_in_use", lambda machine, indices: [0])
    with pytest.raises(RuntimeError, match="gpu busy on"):
        drv.launch(_spec(gpu_indices=[0, 1], port=28200))


def test_an_idempotent_relaunch_skips_the_probe(monkeypatch):
    # A retry of the SAME run reuses its own cards; its residual memory is not a
    # collision, so the probe is skipped when the run's own container is still
    # there. If the probe ran it would (wrongly) refuse; assert it does not.
    drv = SshDockerDriver()
    monkeypatch.setattr(drv, "_check_port_free", lambda *a, **k: None)
    monkeypatch.setattr(
        drv, "attach",
        lambda spec: object(),  # this run already has a container here
    )
    monkeypatch.setattr(
        drv, "gpus_in_use",
        lambda *a: (_ for _ in ()).throw(AssertionError("probe must be skipped on relaunch")),
    )
    monkeypatch.setattr(drv, "_dump_remote_logs", lambda *a, **k: None)
    calls: list[str] = []
    monkeypatch.setattr(drv, "_ssh", lambda machine, cmd, **k: calls.append(cmd) or _completed(""))
    drv.launch(_spec(gpu_indices=[0, 1], port=28200))
    assert any("docker run" in c or "mkdir" in c for c in calls)


def test_gpu_busy_is_infrastructure_not_a_config_verdict():
    # The message is worded to land on gpu_busy, never port_conflict's
    # "already in use" needle, and to be retried on fresh hardware.
    assert classify_failure("gpu busy on 10.0.0.1: cards [0] still hold memory") == "gpu_busy"
    assert is_infrastructure("gpu_busy")
