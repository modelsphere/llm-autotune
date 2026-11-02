"""Ascend resources follow the same launch, placement and provenance path as GPUs."""

import pytest

from app.control.launch.base import MachineInfo
from app.control.launch.k8s import render_policy_job, render_preflight_job, render_workload
from app.hardware import coerce_gpu_type, normalize_gpu_type
from tests.test_k8s_driver import FakeK8sApi, _clone, _driver, _spec
from tests.test_k8s_policy_job import _spec as _policy_spec

RESOURCE = "huawei.com/npu"
PRODUCT_LABEL = "node.kubernetes.io/npu.chip.name"
MACHINE = MachineInfo(
    name="npu-pool", host="", driver="k8s", gpu_count=8,
    node_selector=f"{PRODUCT_LABEL}=910B4-1",
)


def _settings(**over):
    return _clone(**{
        "k8s_workload_kind": "deployment", "k8s_gpu_resource": RESOURCE,
        "k8s_gpu_product_label": PRODUCT_LABEL, "k8s_runtime_class": "", **over,
    })


def _node(count=8):
    return {
        "metadata": {"name": "npu-1", "labels": {
            "kubernetes.io/hostname": "npu-1", PRODUCT_LABEL: "910B4-1",
        }},
        "status": {
            # A second resource represents another allocation mode; never sum it.
            "allocatable": {RESOURCE: str(count), "huawei.com/npu.shared": "80"},
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }


@pytest.mark.parametrize("raw", [
    "910B4-1", "Ascend910B4-1", "Ascend 910B4-1", "ASCEND-910B4-1",
    "huawei-Ascend910B4-1", "910b4_1",
])
def test_chip_names_normalize_without_losing_the_variant(raw):
    assert coerce_gpu_type(raw) == "ASCEND-910B4-1"


def test_other_variants_are_not_silently_conflated():
    assert normalize_gpu_type("910B4-10") == ""
    assert normalize_gpu_type("910B3") == ""


@pytest.mark.parametrize("runtime", ["", "ascend"])
def test_engine_and_policy_request_npus_and_the_configured_runtime(runtime):
    settings = _settings(k8s_runtime_class=runtime)
    pod = render_workload(_spec(machine=MACHINE, engine="vllm"), settings)[0][
        "spec"]["template"]["spec"]
    assert pod.get("runtimeClassName", "") == runtime
    assert pod["containers"][0]["resources"]["limits"][RESOURCE] == 4
    assert pod["nodeSelector"] == {PRODUCT_LABEL: "910B4-1"}
    assert not any(e["name"].startswith("NVIDIA_") for e in pod["containers"][0]["env"])
    assert pod["tolerations"][0]["key"] == RESOURCE
    policy = render_policy_job(_policy_spec(machine=MACHINE, gpu_indices=[0, 1]), settings)[
        "spec"]["template"]["spec"]
    assert policy["containers"][0]["resources"]["limits"][RESOURCE] == 2
    assert policy.get("runtimeClassName", "") == runtime


@pytest.mark.parametrize("runtime", ["", "ascend", "nvidia"])
def test_custom_mode_explicitly_passes_runtime_including_empty(runtime):
    settings = _settings(k8s_workload_kind="custom", k8s_runtime_class=runtime)
    cr = render_workload(_spec(machine=MACHINE), settings)[0]
    assert cr["spec"]["gpuResourceName"] == RESOURCE
    assert cr["spec"]["runtimeClassName"] == runtime
    cpu = _spec(machine=MachineInfo(name="cpu", host="", gpu_count=0), gpu_indices=[])
    assert render_workload(cpu, settings)[0]["spec"]["runtimeClassName"] == ""


def test_capacity_counts_selected_resource_and_free_cards_and_records_type():
    settings = _settings()
    api = FakeK8sApi(settings)
    api.nodes = [_node()]
    api.list_cluster_pods = lambda: [{"spec": {
        "nodeName": "npu-1", "containers": [{"resources": {"limits": {RESOURCE: 3}}}],
    }}]
    driver = _driver(settings, api)
    result = driver.probe_capacity(MACHINE)
    assert (result["gpu_count"], result["node_gpu_max"], result["gpu_free"]) == (8, 8, 5)
    assert result["gpu_type"] == "ASCEND-910B4-1"
    assert result["warnings"] == []
    handle, _ = driver.launch(_spec(machine=MACHINE))
    api.pods = [{"spec": {"nodeName": "npu-1"}}]
    assert driver.environment(handle)["card_type"] == "ASCEND-910B4-1"


@pytest.mark.parametrize("wanted,verdict", [(4, "unschedulable"), (9, "unplaceable")])
def test_busy_npu_node_is_not_mistaken_for_zero_capacity(wanted, verdict):
    settings = _settings()
    api = FakeK8sApi(settings)
    api.nodes = [_node()]
    driver = _driver(settings, api)
    handle, _ = driver.launch(_spec(machine=MACHINE))
    pod = {"spec": {"containers": [{"resources": {"limits": {RESOURCE: wanted}}}]}}
    assert driver._placement_verdict(handle, pod, "NPU busy")[0] == verdict


def test_smoke_and_preflight_read_npu_capacity(monkeypatch):
    settings = _settings()
    api = FakeK8sApi(settings)
    api.nodes = [_node()]
    driver = _driver(settings, api)
    _, check = driver._smoke_nodes(api, MACHINE)
    assert "8 × ASCEND-910B4-1" in check["detail"]
    # Image/model probing has its own tests; isolate the card-width preflight here.
    monkeypatch.setattr(driver, "_probe_job", lambda *args: [])
    checks = driver.preflight(
        MACHINE, image="vllm-ascend:test", model_path="/weights", widest_candidate_cards=8,
    )
    assert next(c for c in checks if c.key == "gpus").status == "pass"


def test_npu_image_preflight_does_not_inject_nvidia_environment():
    job, _ = render_preflight_job(
        "probe-npu", MACHINE, [("engine", "", "", "vllm-ascend:test")], "", _settings(),
    )
    assert job["spec"]["template"]["spec"]["containers"][0]["env"] == []
