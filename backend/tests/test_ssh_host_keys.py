"""Host-key policy on the path to a GPU machine.

Every machine-side action — preflight, capture, clear, launch, teardown, the
GPU probe — goes through `SshDockerDriver._ssh`, so the policy is set once
here. It is deliberately permissive: the fleet is an internal network of boxes
that are re-imaged and re-addressed, and under BatchMode an unknown host is a
hard failure, which is how a freshly leased machine silently stalled every
submission queued for it.
"""

import subprocess

from app.control.launch import MachineInfo
from app.control.launch.ssh_docker import SshDockerDriver


def _ssh_argv(monkeypatch) -> list[str]:
    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    SshDockerDriver()._ssh(MachineInfo(name="gpu-01", host="10.0.0.1", gpu_count=8), "true")
    return seen[0]


def _opt(argv: list[str], key: str) -> str | None:
    for i, token in enumerate(argv):
        if token == "-o" and i + 1 < len(argv) and argv[i + 1].startswith(f"{key}="):
            return argv[i + 1].split("=", 1)[1]
    return None


def test_an_unknown_host_key_never_blocks_a_machine_action(monkeypatch):
    argv = _ssh_argv(monkeypatch)
    assert _opt(argv, "StrictHostKeyChecking") == "no"
    assert _opt(argv, "BatchMode") == "yes"


def test_known_hosts_is_discarded_rather_than_written(monkeypatch):
    """$HOME/.ssh is mounted read-only into the api and worker containers, so
    ssh recording a new key fails and warns on stderr — and stderr is what a
    failed launch reports. It also makes the setting mean what it says: with a
    real known_hosts, a CHANGED key refuses even under StrictHostKeyChecking=no.
    """
    assert _opt(_ssh_argv(monkeypatch), "UserKnownHostsFile") == "/dev/null"


def test_the_permanently_added_warning_stays_out_of_error_messages(monkeypatch):
    assert _opt(_ssh_argv(monkeypatch), "LogLevel") == "ERROR"
