"""A bare-metal machine's cards are read with nvidia-smi, not typed."""

import subprocess

import pytest

from app.control.launch.ssh_docker import SshDockerDriver

PROBE = SshDockerDriver.__dict__["probe_capacity"]


@pytest.fixture
def driver(monkeypatch):
    out: dict = {}
    d = SshDockerDriver.__new__(SshDockerDriver)
    monkeypatch.setattr(
        d, "_ssh",
        lambda machine, cmd, timeout=60: subprocess.CompletedProcess(
            cmd, out.get("code", 0), out.get("stdout", ""), out.get("stderr", "")),
        raising=False,
    )
    d.out = out
    return d


class _Machine:
    name = "node-24"


def test_eight_h100s_are_counted_and_typed(driver, monkeypatch):
    # The fixture stubs the probe for API tests; call the real one here.
    driver.out["stdout"] = "NVIDIA H100 80GB HBM3\n" * 8
    got = PROBE(driver, _Machine())
    assert (got["supported"], got["gpu_count"], got["node_count"]) == (True, 8, 1)
    assert got["gpu_type"]
    assert not got["warnings"]


def test_an_unknown_card_is_counted_but_not_typed(driver):
    driver.out["stdout"] = "Mystery Accelerator 9000\n" * 2
    got = PROBE(driver, _Machine())
    assert (got["gpu_count"], got["gpu_type"]) == (2, "")
    assert "unknown card" in got["warnings"][0]


def test_a_machine_without_nvidia_smi_is_not_probed(driver):
    driver.out.update(code=127, stderr="nvidia-smi: command not found")
    got = PROBE(driver, _Machine())
    assert got["supported"] is False
    assert "command not found" in got["warnings"][0]
