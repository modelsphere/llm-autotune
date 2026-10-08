"""The machine smoke test: each substrate walks what a launch needs, in order,
and the first step a launch could not get past is the one reported as failed."""

import subprocess

import pytest

from app.control.launch import k8s as k8s_mod
from app.control.launch.base import MachineInfo
from app.control.launch.k8s import K8sDriver
from app.control.launch.k8s_api import K8sApi, K8sError
from app.control.launch.ssh_docker import SshDockerDriver
from app.core.config import get_settings


class SmokeApi(K8sApi):
    """A cluster that answers the smoke test's questions from fields."""

    def __init__(self):
        self.pods_error: Exception | None = None
        self.nodes_error: Exception | None = None
        self.nodes: list[dict] = []
        self.denied: set[str] = set()
        self.schedule_onto: str | None = "gpu-node-1"
        self.unschedulable_message = ""
        self.objects: dict[tuple[str, str], dict] = {}
        self.deleted: list[tuple[str, str]] = []

    def apply(self, manifest):
        assert manifest["kind"] == "Pod"
        pod = dict(manifest)
        if self.schedule_onto:
            pod["spec"] = {**manifest["spec"], "nodeName": self.schedule_onto}
            pod["status"] = {"hostIP": "10.0.0.81"}
        else:
            pod["status"] = {"conditions": [{
                "type": "PodScheduled", "status": "False",
                "message": self.unschedulable_message,
            }]}
        self.objects[("pod", manifest["metadata"]["name"])] = pod

    def get(self, resource, name):
        return self.objects.get((resource, name))

    def delete(self, resource, name):
        self.deleted.append((resource, name))
        self.objects.pop((resource, name), None)

    def list(self, resource, label_selector=""):
        if resource == "pods":
            if self.pods_error:
                raise self.pods_error
            return [{}, {}]
        if resource == "nodes":
            if self.nodes_error:
                raise self.nodes_error
            return list(self.nodes)
        return []

    def can_i(self, verb, resource, group=""):
        return f"{verb} {resource}" not in self.denied

    def logs(self, label_selector, tail=200):
        return ""


def _driver(api: SmokeApi, **over) -> K8sDriver:
    settings = get_settings().model_copy(update={
        "k8s_api_mode": "client", "k8s_namespace": "gpu-test",
        "k8s_workload_kind": "deployment", **over,
    })
    return K8sDriver(api=api, settings=settings)


def _machine(**over) -> MachineInfo:
    base = dict(name="gpu-node-1", host="k8s", gpu_count=8, driver="k8s",
                node_selector="kubernetes.io/hostname=gpu-node-1")
    return MachineInfo(**{**base, **over})


def _node(name="gpu-node-1", ip="10.0.0.81", ready=True, cordoned=False) -> dict:
    return {
        "metadata": {"name": name, "labels": {"nvidia.com/gpu.product": "NVIDIA-B300-SXM6-AC"}},
        "spec": {"unschedulable": cordoned},
        "status": {
            "allocatable": {"nvidia.com/gpu": "8"},
            "addresses": [{"type": "InternalIP", "address": ip}],
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
        },
    }


def _by_name(result) -> dict[str, dict]:
    return {check["name"]: check for check in result["checks"]}


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(k8s_mod, "_tcp_reachable", lambda host, port, timeout=3.0: True)
    monkeypatch.setattr(k8s_mod, "_SMOKE_POD_WAIT_SECONDS", 0)
    monkeypatch.setattr(k8s_mod.time, "sleep", lambda _s: None)


def test_a_rejected_credential_stops_the_test_and_says_what_to_fix():
    api = SmokeApi()
    api.pods_error = K8sError("client list pods failed: Unauthorized")
    result = _driver(api, k8s_node_host="10.0.0.84").smoke_test(_machine())
    assert result["ok"] is False
    assert [c["name"] for c in result["checks"]] == ["cluster", "credential"]
    assert "fresh kubeconfig" in result["checks"][-1]["detail"]


def test_an_unconfigured_cluster_fails_first():
    result = _driver(SmokeApi(), k8s_api_mode="unavailable").smoke_test(_machine())
    assert result["ok"] is False
    assert result["checks"][0]["name"] == "cluster"


def test_a_guest_credential_passes_but_cannot_confirm_the_node():
    api = SmokeApi()
    api.nodes_error = K8sError("client list nodes failed: Forbidden")
    result = _driver(api, k8s_node_host="10.0.0.85").smoke_test(_machine())
    checks = _by_name(result)
    assert result["ok"] is True
    assert checks["credential"]["status"] == "pass"
    assert checks["node"]["status"] == "skip"
    assert "probe pod" in checks["node"]["detail"]
    assert checks["endpoint"]["status"] == "pass"


def test_missing_rbac_is_named():
    api = SmokeApi()
    api.nodes = [_node()]
    api.denied = {"create services"}
    result = _driver(api).smoke_test(_machine())
    checks = _by_name(result)
    assert result["ok"] is False
    assert checks["permissions"]["status"] == "fail"
    assert "create services" in checks["permissions"]["detail"]


def test_readable_nodes_report_readiness_and_capacity():
    api = SmokeApi()
    api.nodes = [_node()]
    result = _driver(api).smoke_test(_machine())
    checks = _by_name(result)
    assert result["ok"] is True
    assert checks["node"]["detail"] == "gpu-node-1: Ready, 8 × B300 allocatable"
    # No node_host: the endpoint comes from the node's own IP.
    assert "10.0.0.81" in checks["endpoint"]["detail"]


def test_a_cordoned_node_warns_and_a_missing_one_fails():
    api = SmokeApi()
    api.nodes = [_node(cordoned=True)]
    assert _by_name(_driver(api).smoke_test(_machine()))["node"]["status"] == "warn"
    api.nodes = []
    result = _driver(api).smoke_test(_machine())
    assert result["ok"] is False
    assert "matches no node" in _by_name(result)["node"]["detail"]


def test_a_node_host_on_another_node_is_pointed_out():
    api = SmokeApi()
    api.nodes = [_node()]
    detail = _by_name(_driver(api, k8s_node_host="10.0.0.84").smoke_test(_machine()))[
        "endpoint"]["detail"]
    assert "not this machine's node" in detail


def test_an_unroutable_endpoint_host_warns(monkeypatch):
    monkeypatch.setattr(k8s_mod, "_tcp_reachable", lambda host, port, timeout=3.0: False)
    api = SmokeApi()
    api.nodes_error = K8sError("Forbidden")
    check = _by_name(_driver(api, k8s_node_host="10.9.9.9").smoke_test(_machine()))["endpoint"]
    assert check["status"] == "warn"


def test_no_endpoint_host_at_all_fails():
    api = SmokeApi()
    api.nodes_error = K8sError("Forbidden")
    result = _driver(api, k8s_node_host="").smoke_test(_machine())
    assert result["ok"] is False
    assert _by_name(result)["endpoint"]["status"] == "fail"


def test_the_probe_pod_lands_on_the_node_and_is_removed():
    api = SmokeApi()
    api.nodes_error = K8sError("Forbidden")
    result = _driver(api, k8s_node_host="10.0.0.85").smoke_test(_machine(), probe_pod=True)
    check = _by_name(result)["probe pod"]
    assert check["status"] == "pass"
    assert check["detail"] == "scheduled onto gpu-node-1 (10.0.0.81)"
    assert api.deleted and api.deleted[0][0] == "pod"
    assert not api.objects


def test_the_probe_pod_carries_the_engines_placement():
    api = SmokeApi()
    seen = {}
    real_apply = api.apply

    def capture(manifest):
        seen.update(manifest)
        real_apply(manifest)

    api.apply = capture
    _driver(api, k8s_node_host="h", k8s_tolerations="node.kubernetes.io/unschedulable").smoke_test(
        _machine(), probe_pod=True
    )
    spec = seen["spec"]
    assert spec["nodeSelector"] == {"kubernetes.io/hostname": "gpu-node-1"}
    keys = {t["key"] for t in spec["tolerations"]}
    assert {"nvidia.com/gpu", "node.kubernetes.io/unschedulable"} <= keys
    assert "nvidia.com/gpu" not in str(spec["containers"][0]["resources"])


def test_an_unschedulable_probe_pod_fails_with_the_schedulers_reason():
    api = SmokeApi()
    api.nodes_error = K8sError("Forbidden")
    api.schedule_onto = None
    api.unschedulable_message = "0/10 nodes are available: 1 node(s) had untolerated taint(s)"
    result = _driver(api, k8s_node_host="h").smoke_test(_machine(), probe_pod=True)
    check = _by_name(result)["probe pod"]
    assert result["ok"] is False
    assert "untolerated taint" in check["detail"]
    assert not api.objects


# -- ssh_docker -----------------------------------------------------------------


def _ssh_driver(monkeypatch, answers: dict[str, tuple[int, str, str]]):
    def fake(self, machine, cmd, timeout=60):
        for prefix, (rc, out, err) in answers.items():
            if cmd.startswith(prefix):
                return subprocess.CompletedProcess(cmd, rc, out, err)
        raise AssertionError(f"unexpected ssh {cmd!r}")

    monkeypatch.setattr(SshDockerDriver, "_ssh", fake)
    return SshDockerDriver()


def _box(**over) -> MachineInfo:
    return MachineInfo(**{"name": "nv-50", "host": "10.0.0.50", "gpu_count": 8, **over})


def test_ssh_failure_stops_the_test(monkeypatch):
    driver = _ssh_driver(monkeypatch, {"true": (255, "", "Permission denied (publickey)")})
    result = driver.smoke_test(_box())
    assert result["ok"] is False
    assert [c["name"] for c in result["checks"]] == ["ssh"]
    assert "Permission denied" in result["checks"][0]["detail"]


def test_a_healthy_box_passes_every_step(monkeypatch):
    driver = _ssh_driver(monkeypatch, {
        "true": (0, "", ""),
        "docker info": (0, "27.1.1\n", ""),
        "nvidia-smi": (0, "NVIDIA H100 80GB HBM3\n" * 8, ""),
    })
    result = driver.smoke_test(_box())
    assert result["ok"] is True
    assert _by_name(result)["gpus"]["detail"] == "8 × H100 visible"


def test_fewer_gpus_than_recorded_warns(monkeypatch):
    driver = _ssh_driver(monkeypatch, {
        "true": (0, "", ""),
        "docker info": (0, "27.1.1\n", ""),
        "nvidia-smi": (0, "NVIDIA H100 80GB HBM3\n" * 7, ""),
    })
    check = _by_name(driver.smoke_test(_box()))["gpus"]
    assert check["status"] == "warn"
    assert "records 8" in check["detail"]
