"""The LLMService driver, over an in-memory cluster.

One run is one LLMService; the operator installs the chart and says nothing
about the engine, so readiness, failures and logs come from the pods and the
chart's Service, which these tests stand in for."""

import pytest

from app.control.launch import get_driver
from app.control.launch.base import DeploymentState, LaunchSpec, MachineInfo
from app.control.launch.failures import classify_failure, is_config_induced, is_infrastructure
from app.control.launch.k8s_api import LLMSVC_RESOURCE
from app.control.launch.llmservice import LLMServiceDriver
from app.control.recipe.compose import RUN_LABEL
from app.core.config import get_settings
from tests.test_k8s_driver import FakeK8sApi


class LlmsvcApi(FakeK8sApi):
    def _resource_of(self, manifest):
        if manifest.get("kind") == "LLMService":
            return LLMSVC_RESOURCE
        return super()._resource_of(manifest)

    def list(self, resource, label_selector=""):
        if resource == "pods":
            return [p for p in self.pods if _matches(p, label_selector)]
        return super().list(resource, label_selector)


def _matches(pod, selector):
    if not selector:
        return True
    key, _, value = selector.partition("=")
    return ((pod.get("metadata") or {}).get("labels") or {}).get(key) == value


def _settings(**over):
    base = dict(k8s_workload_kind="llmservice", k8s_namespace="runs", k8s_api_mode="client")
    base.update(over)
    return get_settings().model_copy(update=base)


def _spec(**over):
    base = dict(
        run_id=7,
        machine=MachineInfo(name="pool", host="pool", gpu_count=8, driver="k8s"),
        engine="sglang",
        image="lmsysorg/sglang:v0.5.20",
        model_path="/mnt/disk0/models/qwen",
        served_model_name="qwen",
        engine_args={"tp": 2, "mem_fraction_static": 0.85},
        env={"A": "1"},
    )
    base.update(over)
    return LaunchSpec(**base)


def _driver(**over):
    settings = _settings(**over)
    api = LlmsvcApi(settings)
    return LLMServiceDriver(api=api, settings=settings), api


def _pod(ready=False, waiting=""):
    status = {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}
    if waiting:
        status["containerStatuses"] = [
            {"name": "sglang", "state": {"waiting": {"reason": waiting}}}
        ]
    return {
        "metadata": {"name": "autotune-run-7-x", "labels": {RUN_LABEL: "7"}, "uid": "u1"},
        "spec": {
            "nodeName": "n1",
            "containers": [{"name": "sglang", "image": "lmsysorg/sglang:v0.5.20"}],
        },
        "status": status,
    }


def test_a_run_is_one_llmservice_with_the_recipe_as_its_catalog_layer():
    driver, api = _driver()
    handle, command = driver.launch_nodes(_spec())
    (obj,) = api.applied
    assert obj["kind"] == "LLMService" and obj["metadata"]["name"] == "autotune-run-7"
    layers = {layer["name"]: layer["values"] for layer in obj["spec"]["layers"]}
    assert layers["catalog"]["extraArgs"] == ["--mem-fraction-static=0.85", "--tp-size=2"]
    assert layers["catalog"]["model"]["gpus"] == "2"
    assert layers["site"]["model"]["localPath"] == "/mnt/disk0/models/qwen"
    assert layers["autotune"]["podLabels"][RUN_LABEL] == "7"
    assert obj["spec"]["chart"]["repo"] == "https://modelsphere.github.io/helm-charts"
    # In-cluster DNS is the default endpoint.
    assert handle.endpoint_url == "http://autotune-run-7.runs.svc:30000"
    assert command[0].startswith("kubectl apply -f -")


def test_ready_is_the_front_door_having_an_endpoint():
    driver, api = _driver()
    handle, _ = driver.launch_nodes(_spec())
    api.pods = [_pod(ready=True)]
    # Pods ready but the Service has no ready address yet: still coming up.
    assert driver.state(handle) == DeploymentState.STARTING
    api.objects[("endpoints", "autotune-run-7")] = {
        "subsets": [{"addresses": [{"ip": "10.0.0.1"}]}]
    }
    assert driver.state(handle) == DeploymentState.READY


def test_a_crash_looping_engine_is_crashed():
    driver, api = _driver()
    handle, _ = driver.launch_nodes(_spec())
    api.pods = [_pod(waiting="CrashLoopBackOff")]
    assert driver.state(handle) == DeploymentState.CRASHED


@pytest.mark.parametrize(
    "reason,message,kind",
    [
        (
            "ApplyFailed",
            "execution error at (sglang/templates/lws.yaml): sglang: pd and lws",
            "bad_config",
        ),
        ("ChartResolveFailed", "no version of sglang matches >=9", "cluster_setup"),
        ("ChartRepoNotAllowed", "repo not allowed", "cluster_setup"),
    ],
)
def test_the_operators_final_verdicts(reason, message, kind):
    driver, api = _driver()
    handle, _ = driver.launch_nodes(_spec())
    obj = api.objects[(LLMSVC_RESOURCE, "autotune-run-7")]
    obj["status"] = {
        "phase": "Failed",
        "message": message,
        "conditions": [{"type": "Applied", "status": "False", "reason": reason}],
    }
    assert driver.state(handle) == DeploymentState.CRASHED
    failure_class, text = driver.failure_reason(handle)
    assert failure_class == kind
    assert classify_failure(text) == kind


def test_an_apply_error_the_operator_retries_is_not_final():
    driver, api = _driver()
    handle, _ = driver.launch_nodes(_spec())
    api.objects[(LLMSVC_RESOURCE, "autotune-run-7")]["status"] = {
        "phase": "Failed",
        "message": "the server is currently unable to handle the request",
        "conditions": [{"type": "Applied", "status": "False", "reason": "ApplyFailed"}],
    }
    assert driver.state(handle) == DeploymentState.STARTING


def test_the_failure_classes_mean_what_they_say():
    assert is_config_induced("bad_config")
    assert is_infrastructure("cluster_setup")


def test_gone_means_the_object_and_its_pods():
    driver, api = _driver()
    handle, _ = driver.launch_nodes(_spec())
    api.pods = [_pod()]
    driver.teardown(handle)
    assert api.deleted == [(LLMSVC_RESOURCE, "autotune-run-7")]
    # An operator before 0.5.1 drops the object while the pods terminate.
    assert not driver.is_gone(handle)
    api.pods = []
    assert driver.is_gone(handle)


def test_nodeport_is_opt_in():
    driver, api = _driver(k8s_endpoint="nodeport", k8s_node_host="10.1.1.1")
    handle, _ = driver.launch_nodes(_spec())
    layers = {layer["name"]: layer["values"] for layer in api.applied[0]["spec"]["layers"]}
    assert layers["autotune"]["service"]["type"] == "NodePort"
    # The operator has not made the Service yet: no endpoint until attach.
    assert handle.endpoint_url == ""
    api.objects[("service", "autotune-run-7")] = {"spec": {"ports": [{"nodePort": 31234}]}}
    assert driver.attach(_spec()).endpoint_url == "http://10.1.1.1:31234"


def test_a_config_the_chart_would_refuse_never_launches():
    driver, api = _driver()
    with pytest.raises(RuntimeError) as exc:
        driver.launch_nodes(_spec(image="lmsysorg/sglang"))
    assert classify_failure(str(exc.value)) == "bad_config"
    assert api.applied == []


def test_weights_on_a_pvc_cannot_launch_here():
    driver, _ = _driver(k8s_model_pvc="models")
    with pytest.raises(RuntimeError) as exc:
        driver.launch_nodes(_spec())
    assert classify_failure(str(exc.value)) == "cluster_setup"


def test_site_values_land_in_the_site_layer():
    driver, api = _driver(
        k8s_site_values='{"securityContext": {"capabilities": {"add": ["IPC_LOCK"]}}}',
        k8s_engine_memory_request="64Gi",
        k8s_image_mirror="reg.example/team",
    )
    driver.launch_nodes(_spec())
    site = {layer["name"]: layer["values"] for layer in api.applied[0]["spec"]["layers"]}["site"]
    assert site["securityContext"]["capabilities"]["add"] == ["IPC_LOCK"]
    assert site["resources"]["requests"]["memory"] == "64Gi"
    assert site["image"]["repository"] == "reg.example/team/sglang"


def test_a_stored_recipe_launches_as_it_is():
    from app.control.recipe.legacy import from_fields

    recipe = from_fields(engine="sglang", image="i:t", served_model_name="m", engine_args={"tp": 1})
    recipe.values["extraArgs"].append("--page-size 64")
    driver, api = _driver()
    driver.launch_nodes(_spec(recipe=recipe.model_dump()))
    catalog = api.applied[0]["spec"]["layers"][0]["values"]
    assert "--page-size 64" in catalog["extraArgs"]


def test_the_environment_comes_from_the_engine_container():
    driver, api = _driver()
    handle, _ = driver.launch_nodes(_spec())
    pod = _pod(ready=True)
    pod["status"]["containerStatuses"] = [
        {"name": "hang-watcher", "imageID": "docker.io/x/hw@sha256:" + "b" * 64},
        {"name": "sglang", "imageID": "docker.io/lmsysorg/sglang@sha256:" + "a" * 64},
    ]
    api.pods = [pod]
    env = driver.environment(handle)
    assert env["image_digest"].endswith("a" * 64)
    assert env["k8s_node"] == "n1"


def test_the_factory_picks_this_driver_for_an_llmservice_cluster(monkeypatch):
    import app.control.launch.k8s as k8s_module

    monkeypatch.setattr(
        k8s_module, "resolve_cluster", lambda cluster_id: k8s_module.as_cluster(_settings())
    )
    assert isinstance(get_driver("k8s"), LLMServiceDriver)
