"""Shared test setup."""

import pytest

from app.control.launch.ssh_docker import SshDockerDriver


@pytest.fixture(autouse=True)
def _no_real_ssh_probe(monkeypatch):
    """Saving a machine reads its cards over ssh. Test machines are fake
    addresses, so every save would wait out a connect timeout; the probe's own
    parsing is tested on its own (test_ssh_capacity_probe.py)."""
    monkeypatch.setattr(SshDockerDriver, "probe_capacity",
                        lambda self, machine: {"supported": False})


@pytest.fixture(autouse=True)
def _no_llmbench_estimate(monkeypatch):
    """Creating a campaign asks LLMBench how long its benchmark takes; tests
    have no LLMBench, and the estimate itself is tested on its own."""
    from app.api import campaigns

    async def _none(body):
        return {}

    monkeypatch.setattr(campaigns, "_benchmark_estimates", _none)
