"""The LLMService driver, over an in-memory cluster.

One run is one LLMService; the operator installs the chart and says nothing
about the engine, so readiness, failures and logs come from the pods and the
chart's Service, which these tests stand in for."""

import json

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
        if manifest.get("kind") == "ConfigMap":
            return "configmap"
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
        (
            "ApplyFailed",
            "values don't meet the specifications of the schema(s) in the following "
            "chart(s):\nsglang:\n- at '/lws/restartPolicy': value must be one of 'Default'",
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


def test_a_fixed_nodeport_is_a_service_of_our_own_selecting_the_front_door():
    """A firewall that lets one port through: the chart takes no nodePort, so
    the run gets a NodePort Service of AutoTune's on that port, selecting what
    the chart's Service selects, owned by the LLMService."""
    driver, api = _driver(k8s_endpoint="nodeport", k8s_node_host="10.1.1.1",
                          k8s_service_nodeport=30500)
    handle, _ = driver.launch_nodes(_spec())
    assert handle.endpoint_url == "", "no front door yet: attach resolves it"
    api.objects[(LLMSVC_RESOURCE, "autotune-run-7")] = {"metadata": {"uid": "llmsvc-uid"}}
    api.objects[("service", "autotune-run-7")] = {"spec": {
        "selector": {"app": "autotune-run-7-sglang"},
        "ports": [{"port": 30000, "nodePort": 31234}]}}
    assert driver.attach(_spec()).endpoint_url == "http://10.1.1.1:30500"
    ours = api.applied[-1]
    assert ours["kind"] == "Service" and ours["metadata"]["name"] == "autotune-run-7-np"
    assert ours["spec"]["selector"] == {"app": "autotune-run-7-sglang"}
    assert ours["spec"]["ports"] == [{"name": "http-inference", "port": 30000,
                                      "targetPort": 30000, "nodePort": 30500}]
    assert ours["metadata"]["ownerReferences"][0]["uid"] == "llmsvc-uid"
    applied = len(api.applied)
    driver.attach(_spec())
    assert len(api.applied) == applied, "made once"
    driver.teardown(handle)
    assert ("service", "autotune-run-7-np") in api.deleted


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


def test_the_campaigns_pinned_chart_version_wins_over_the_recipes_range():
    from app.control.recipe.legacy import from_fields

    recipe = from_fields(engine="sglang", image="i:t", served_model_name="m", engine_args={"tp": 1})
    recipe.chart.version = ">=0.7.1"
    driver, api = _driver()
    driver.launch_nodes(_spec(recipe=recipe.model_dump()))
    assert api.applied[0]["spec"]["chart"]["version"] == ">=0.7.1"
    driver.launch_nodes(_spec(run_id=8, recipe=recipe.model_dump(), chart_version="0.9.26"))
    assert api.applied[1]["spec"]["chart"]["version"] == "0.9.26"


def test_the_environment_says_which_chart_the_operator_installed():
    driver, api = _driver()
    handle, _ = driver.launch_nodes(_spec())
    api.objects[(LLMSVC_RESOURCE, "autotune-run-7")]["status"] = {
        "phase": "Applied",
        "chart": {"name": "sglang", "version": "0.9.26"},
    }
    env = driver.environment(handle)
    assert env["chart"] == "sglang-0.9.26" and env["chart_version"] == "0.9.26"


# -- what the cluster adds to a recipe: the card resource, inputs ---------------

ASCEND = "huawei.com/Ascend910B4-1"


def _variant_recipe(**over):
    """A catalog variant as the catalog writes one for Ascend cards today: how
    many cards under requires, no resource name anywhere."""
    from app.control.recipe import Recipe

    doc = {
        "apiVersion": "catalog.swiss/v2",
        "kind": "Variant",
        "model": "qwen",
        "engine": "vllm",
        "chart": {"name": "vllm", "version": ">=0.9.1"},
        "image": {"repository": "quay.io/ascend/vllm-ascend", "tag": "v1"},
        "requires": {"gpus": 2, "topology": "single-node", "vendor": "ascend"},
        "values": {
            "extraArgs": ["--tensor-parallel-size=2"],
            "resources": {"limits": {"cpu": "16"}, "requests": {"cpu": "16"}},
        },
    }
    doc.update(over)
    return Recipe.from_variant(doc)


def _layers(api):
    return {layer["name"]: layer["values"] for layer in api.applied[0]["spec"]["layers"]}


def test_the_cards_are_requested_as_the_clusters_resource():
    driver, api = _driver(k8s_gpu_resource=ASCEND)
    driver.launch_nodes(_spec(recipe=_variant_recipe().model_dump()))
    layers = _layers(api)
    assert layers["site"]["model"]["gpuResource"] == ASCEND
    assert layers["catalog"]["model"] == {"name": "qwen", "gpus": "2"}  # the recipe is untouched


def test_nvidia_cards_render_as_before_the_key_existed():
    driver, api = _driver()
    driver.launch_nodes(_spec())
    assert "gpuResource" not in _layers(api)["site"].get("model", {})


def test_site_values_name_the_resource_where_it_differs_from_the_counted_one():
    # The cluster counts huawei.com/npu; this pool's device plugin hands cards
    # out under another name.
    driver, api = _driver(
        k8s_gpu_resource="huawei.com/npu",
        k8s_site_values=json.dumps({"model": {"gpuResource": ASCEND}}),
    )
    driver.launch_nodes(_spec(recipe=_variant_recipe().model_dump()))
    assert _layers(api)["site"]["model"]["gpuResource"] == ASCEND


def test_a_recipe_that_names_its_resource_or_its_cards_under_resources_keeps_them():
    driver, api = _driver(k8s_gpu_resource=ASCEND)
    own = _variant_recipe()
    own.values["model"]["gpuResource"] = "volcano.sh/Ascend910B4-1"
    driver.launch_nodes(_spec(recipe=own.model_dump()))
    assert "gpuResource" not in _layers(api)["site"].get("model", {})

    driver, api = _driver(k8s_gpu_resource=ASCEND)
    old = _variant_recipe(requires={"gpus": 0, "topology": "single-node"})
    old.values["resources"]["limits"]["volcano.sh/Ascend910B4-1"] = "2"
    driver.launch_nodes(_spec(recipe=old.model_dump()))
    assert "gpuResource" not in _layers(api)["site"].get("model", {})


def test_a_card_count_written_twice_never_launches():
    # model.gpus 2 and the same cards again under resources: the chart refuses
    # it once the cluster's resource is on, minutes into the run. Refused here.
    driver, api = _driver(k8s_gpu_resource=ASCEND)
    twice = _variant_recipe()
    twice.values["resources"]["limits"][ASCEND] = "2"
    with pytest.raises(RuntimeError, match="chart refuses.*model.gpus already requests"):
        driver.launch_nodes(_spec(recipe=twice.model_dump()))
    assert api.applied == []


def test_a_chart_too_old_for_the_resource_key_is_the_clusters_setup():
    driver, api = _driver(k8s_gpu_resource=ASCEND)
    recipe = _variant_recipe().model_dump()
    with pytest.raises(RuntimeError, match="cluster setup:.*vllm 0.9.1 or newer"):
        driver.launch_nodes(_spec(recipe=recipe, chart_version="0.8.23"))
    driver.launch_nodes(_spec(recipe=recipe, chart_version="0.9.4"))
    assert len(api.applied) == 1


INPUTS = [
    {"key": "storageKVOffload.mooncake.master", "title": "Mooncake master", "example": "m:1"},
    {
        "key": "storageKVOffload.mooncake.device",
        "title": "HCAs",
        "example": "x",
        "default": "mlx5_0",
    },
]


def test_an_input_is_answered_by_the_site_or_its_default():
    driver, api = _driver(
        k8s_site_values='{"storageKVOffload": {"mooncake": {"master": "10.0.0.1:50051"}}}'
    )
    recipe = _variant_recipe(inputs=INPUTS)
    driver.launch_nodes(_spec(recipe=recipe.model_dump()))
    assert _layers(api)["site"]["storageKVOffload"]["mooncake"] == {
        "master": "10.0.0.1:50051",
        "device": "mlx5_0",
    }
    assert recipe.to_variant()["inputs"] == INPUTS
    assert recipe.hash() == _variant_recipe().hash()  # the inputs are not the configuration


def test_an_unanswered_input_never_launches():
    driver, api = _driver()
    with pytest.raises(RuntimeError, match=r"cluster setup:.*mooncake\.master \(Mooncake master\)"):
        driver.launch_nodes(_spec(recipe=_variant_recipe(inputs=INPUTS).model_dump()))
    assert api.applied == []


def test_a_cluster_names_its_scheduler_and_what_its_cards_are_requested_as():
    # Counted as huawei.com/npu, handed out by volcano under its own name, and
    # only to pods volcano placed.
    driver, api = _driver(
        k8s_gpu_resource="huawei.com/npu",
        k8s_gpu_request_resource="volcano.sh/Ascend910B4-1",
        k8s_scheduler_name="volcano",
    )
    driver.launch_nodes(_spec(recipe=_variant_recipe().model_dump()))
    site = _layers(api)["site"]
    assert site["model"]["gpuResource"] == "volcano.sh/Ascend910B4-1"
    assert site["schedulerName"] == "volcano"
    # The toleration still follows the resource the nodes are counted (and tainted) by.
    assert {"key": "huawei.com/npu", "operator": "Exists", "effect": "NoSchedule"} in site[
        "tolerations"
    ]

    driver, api = _driver()
    driver.launch_nodes(_spec())
    assert "schedulerName" not in _layers(api)["site"]


def test_a_launchs_files_are_a_configmap_the_chart_mounts():
    """(1.9) Each file is mounted read-only at its path from one ConfigMap,
    made before the engine and then owned by its LLMService; the recipe's own
    volumes are kept."""
    from app.control.recipe import Recipe
    from app.control.recipe.legacy import from_fields

    recipe = from_fields(
        engine="sglang", image="lmsysorg/sglang:v0.5.20", served_model_name="qwen",
        engine_args={"tp": 2}, extra_volumes={"/data/cache": "/cache"},
    )
    files = {"/opt/moe/E=128.json": '{"1": {}}', "/etc/chat.jinja": "{{ messages }}"}
    driver, api = _driver()


    real_apply = api.apply

    def apply(manifest):
        real_apply(manifest)
        if manifest.get("kind") == "LLMService":
            api.objects[(LLMSVC_RESOURCE, "autotune-run-7")]["metadata"]["uid"] = "svc-uid"

    api.apply = apply
    handle, _ = driver.launch_nodes(_spec(recipe=recipe.model_dump(), files=files))
    kinds = [m["kind"] for m in api.applied]
    assert kinds == ["ConfigMap", "LLMService", "ConfigMap"], "made first, then owned"
    made, owned = api.applied[0], api.applied[2]
    assert made["metadata"]["name"] == "autotune-run-7-files"
    assert made["metadata"]["ownerReferences"] == []
    assert owned["metadata"]["ownerReferences"][0]["uid"] == "svc-uid"
    assert owned["data"] == {"f0": "{{ messages }}", "f1": '{"1": {}}'}

    layers = {layer["name"]: layer["values"] for layer in api.applied[1]["spec"]["layers"]}
    assert layers["autotune"]["volumes"] == [
        {"name": "extra-0", "hostPath": {"path": "/data/cache"}},
        {"name": "autotune-files", "configMap": {"name": "autotune-run-7-files"}},
    ]
    assert layers["autotune"]["volumeMounts"] == [
        {"name": "extra-0", "mountPath": "/cache"},
        {"name": "autotune-files", "mountPath": "/etc/chat.jinja", "subPath": "f0",
         "readOnly": True},
        {"name": "autotune-files", "mountPath": "/opt/moe/E=128.json", "subPath": "f1",
         "readOnly": True},
    ]
    assert Recipe.model_validate(recipe.model_dump())  # the stored recipe is untouched

    driver.teardown(handle)
    assert ("configmap", "autotune-run-7-files") in api.deleted


def test_a_launch_without_files_makes_no_configmap():
    driver, api = _driver()
    handle, _ = driver.launch_nodes(_spec())
    assert [m["kind"] for m in api.applied] == ["LLMService"]
    layers = {layer["name"]: layer["values"] for layer in api.applied[0]["spec"]["layers"]}
    assert "volumes" not in layers["autotune"]
