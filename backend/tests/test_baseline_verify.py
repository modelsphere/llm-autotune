"""Restore verification: a restore script can disagree with the container it
was supposed to recreate. Observed live on node-24 — deploy-8050.sh declared
--tp 2 while the captured service was tp=4, so the "restore" silently halved
that deployment."""

from app.control.launch import MachineInfo
from app.control.launch.ssh_docker import SshDockerDriver

MACHINE = MachineInfo(name="node-24", host="10.0.0.1", gpu_count=8)

CAPTURED = {
    "services": [
        {
            "container": "sglang-prod-p8050",
            "image": "reg.example/sglang:v1",
            "port": "8050",
            "served_model_name": "glm-5",
            "cards": 4,
        }
    ]
}


def _driver_seeing(services: list[dict]) -> SshDockerDriver:
    driver = SshDockerDriver()
    driver.capture_baseline = lambda machine: {"services": services}  # type: ignore[method-assign]
    return driver


def test_faithful_restore_reports_match():
    driver = _driver_seeing(
        [{"port": "8050", "image": "reg.example/sglang:v1",
          "served_model_name": "glm-5", "cards": 4}]
    )
    findings = driver.verify_baseline(MACHINE, CAPTURED)
    assert findings == [{"port": "8050", "ok": True, "detail": "matches capture"}]


def test_parallelism_drift_is_flagged():
    driver = _driver_seeing(
        [{"port": "8050", "image": "reg.example/sglang:v1",
          "served_model_name": "glm-5", "cards": 2}]
    )
    findings = driver.verify_baseline(MACHINE, CAPTURED)
    assert findings[0]["ok"] is False
    assert "was 4 card(s), now 2" in findings[0]["detail"]


def test_missing_service_is_flagged():
    findings = _driver_seeing([]).verify_baseline(MACHINE, CAPTURED)
    assert findings[0]["ok"] is False
    assert "no service listening" in findings[0]["detail"]


def test_image_and_model_drift_are_flagged():
    driver = _driver_seeing(
        [{"port": "8050", "image": "reg.example/sglang:v2",
          "served_model_name": "other", "cards": 4}]
    )
    detail = driver.verify_baseline(MACHINE, CAPTURED)[0]["detail"]
    assert "image:" in detail and "served model:" in detail


# -- restore never stomps a service we did not start --------------------------

RESTORABLE = {
    "services": [
        {
            "container": "sglang-prod-p8050",
            "port": "8050",
            "endpoint_url": "http://10.0.0.1:8050",
            "docker_run": "docker run -d --name sglang-prod-p8050 reg.example/sglang:v1",
        }
    ]
}


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _restoring_driver(*, endpoint_up=False, ps_shows=None):
    driver = SshDockerDriver()
    driver._calls = []  # type: ignore[attr-defined]

    def fake_ssh(machine, command, timeout=None, **_):
        driver._calls.append(command)  # type: ignore[attr-defined]
        if command.startswith("docker ps"):
            return _Result(0, "\n".join(ps_shows or []))
        return _Result(0, "")

    driver._ssh = fake_ssh  # type: ignore[method-assign]
    driver._endpoint_ready = lambda url: endpoint_up  # type: ignore[method-assign]
    return driver


def test_restore_is_a_no_op_when_production_is_already_answering():
    """Don't kill what we did not start: a live endpoint means production is
    back — leave it, never tear it down to relaunch our copy."""
    driver = _restoring_driver(endpoint_up=True)
    restored = driver.restore_baseline(MACHINE, RESTORABLE)
    assert restored == ["sglang-prod-p8050"]
    assert not any(
        "docker rm" in c or "docker run" in c for c in driver._calls  # type: ignore[attr-defined]
    ), "a live service must not be torn down"


def test_restore_is_a_no_op_when_the_container_is_already_running():
    """Even if the endpoint is not answering yet, a container already up under
    the captured name is not ours to remove."""
    driver = _restoring_driver(endpoint_up=False, ps_shows=["sglang-prod-p8050"])
    restored = driver.restore_baseline(MACHINE, RESTORABLE)
    assert restored == ["sglang-prod-p8050"]
    assert not any(
        "docker rm" in c or "docker run" in c for c in driver._calls  # type: ignore[attr-defined]
    ), "a running container must not be rm'd and relaunched"


def test_restore_starts_the_service_when_nothing_is_up():
    """The other direction: when production really is down, restore brings it
    back from the captured command."""
    driver = _restoring_driver(endpoint_up=False, ps_shows=[])
    restored = driver.restore_baseline(MACHINE, RESTORABLE)
    assert restored == ["sglang-prod-p8050"]
    assert any(
        "docker run -d" in c for c in driver._calls  # type: ignore[attr-defined]
    ), "a down service must actually be started"
