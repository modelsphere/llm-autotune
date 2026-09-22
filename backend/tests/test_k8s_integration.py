"""Live k8s integration — the driver against a real cluster, no fake.

This is the test the fake cannot be: it exercises the actual `kubectl` transport
(apply a Deployment + Service, get, list pods, logs, delete) and the real
status → DeploymentState mapping, end to end, on a throwaway cluster.

It uses a *stub* engine (a tiny HTTP server that answers `/v1/models` with 200)
because the point is the driver's lifecycle, not sglang — there is no GPU here.
The engine command is the only thing swapped out; render, apply, poll, attach,
teardown all run for real.

Opt-in and self-skipping: runs only when `AUTOTUNE_K8S_IT=1` AND a cluster is
reachable, so it never touches an unexpected context. Locally:

    AUTOTUNE_K8S_IT=1 uv run pytest tests/test_k8s_integration.py -q

against e.g. a k3d cluster with `python:3.11-slim` imported.
"""

import os
import subprocess
import time

import pytest

from app.control.launch.base import DeploymentState, LaunchSpec, MachineInfo
from app.control.launch.k8s import K8sDriver
from app.core.config import get_settings

NAMESPACE = "autotune-it"
STUB_IMAGE = "python:3.11-slim"
STUB_PORT = 8080

# A 200-on-every-path HTTP server, so kubelet's /v1/models readiness probe
# passes. Prints one line so `logs()` has something to return.
STUB_SERVER = (
    "import http.server,socketserver;"
    "print('stub-engine up',flush=True);"
    "H=type('H',(http.server.BaseHTTPRequestHandler,),{"
    "'do_GET':lambda s:(s.send_response(200),s.end_headers(),"
    "s.wfile.write(b'{\"object\":\"list\",\"data\":[]}')),"
    "'log_message':lambda s,*a:None});"
    f"socketserver.TCPServer(('',{STUB_PORT}),H).serve_forever()"
)


class _StubAdapter:
    """Stands in for the sglang/vllm adapter so the container runs the stub
    server instead of an engine that needs a GPU and weights."""

    def build_command(self, spec):
        return ["python3", "-c", STUB_SERVER]


def _cluster_reachable() -> bool:
    try:
        return subprocess.run(
            ["kubectl", "cluster-info", "--request-timeout=5s"],
            capture_output=True, timeout=10,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


pytestmark = pytest.mark.skipif(
    os.environ.get("AUTOTUNE_K8S_IT") != "1" or not _cluster_reachable(),
    reason="set AUTOTUNE_K8S_IT=1 and point kubectl at a throwaway cluster",
)


@pytest.fixture
def namespace():
    subprocess.run(["kubectl", "create", "namespace", NAMESPACE], capture_output=True)
    yield NAMESPACE
    # Deleting the namespace removes the Deployment + Service + pods together.
    subprocess.run(
        ["kubectl", "delete", "namespace", NAMESPACE, "--wait=false"], capture_output=True
    )


@pytest.fixture
def driver(namespace, monkeypatch):
    settings = get_settings().model_copy(update={
        "k8s_api_mode": "kubectl",
        "k8s_workload_kind": "deployment",
        "k8s_namespace": namespace,
    })
    # Swap only the engine command; everything else in render_workload is real.
    monkeypatch.setattr("app.control.launch.k8s.get_adapter", lambda engine: _StubAdapter())
    return K8sDriver(settings=settings)


def _spec():
    return LaunchSpec(
        run_id=1,
        # gpu_count=0 → no GPU request / runtimeClass, so it schedules on a
        # plain node. model_path=/tmp mounts a dir that exists on the node.
        machine=MachineInfo(name="local", host="127.0.0.1", gpu_count=0, driver="k8s"),
        engine="sglang",
        image=STUB_IMAGE,
        model_path="/tmp",
        served_model_name="stub",
        engine_args={},
        gpu_indices=[],
        port=STUB_PORT,
    )


def _wait_for(driver, handle, target, timeout=150):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = driver.state(handle)
        if last == target:
            return last
        if last == DeploymentState.CRASHED and target != DeploymentState.CRASHED:
            pytest.fail(f"workload CRASHED waiting for {target}; logs:\n{driver.logs(handle)}")
        time.sleep(3)
    pytest.fail(f"timed out waiting for {target}; last state {last}")


def test_full_lifecycle_on_a_real_cluster(driver):
    spec = _spec()

    # launch → both objects applied, a usable handle back.
    handle, command = driver.launch(spec)
    assert command.startswith("kubectl apply -f -")
    assert handle.container_name == "autotune-run-1"
    assert handle.endpoint_url.startswith("http://")  # NodePort endpoint resolved

    # attach re-finds it by name (crash-recovery path).
    assert driver.attach(spec) is not None

    # poll to READY — driven by kubelet's real /v1/models readiness probe.
    _wait_for(driver, handle, DeploymentState.READY)

    # provenance + logs come off the live workload.
    env = driver.environment(handle)
    assert env.get("image_tag") == STUB_IMAGE
    assert env.get("k8s_node")
    assert "stub-engine up" in driver.logs(handle)

    # teardown → both objects gone, idempotently.
    driver.teardown(handle)
    _wait_for(driver, handle, DeploymentState.GONE, timeout=60)
    assert driver.attach(spec) is None
    driver.teardown(handle)  # second time is a no-op
