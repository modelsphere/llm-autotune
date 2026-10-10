"""A cluster machine's campaign preflight: a probe Job on the machine's node
pulls every image the campaign runs and mounts the model as a run would, and
the pod's own state says what a run would have hit."""

from app.control.launch.base import MachineInfo
from app.control.launch.k8s import PREFLIGHT_LABEL, K8sDriver, render_preflight_job
from app.control.launch.k8s_api import K8sApi
from app.core.config import get_settings

ENGINE = "registry.example.com/sglang:v1"
POLICY = "registry.example.com/policy-random-search:0.1.0"
MODEL = "/mnt/models/qwen"


def _node() -> dict:
    return {
        "metadata": {"name": "gpu-node-1", "labels": {}},
        "spec": {},
        "status": {
            "allocatable": {"nvidia.com/gpu": "8"},
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }


def _terminated(name: str) -> dict:
    return {"name": name, "state": {"terminated": {"exitCode": 0, "reason": "Completed"}}}


def _waiting(name: str, reason: str, message: str = "") -> dict:
    return {"name": name, "state": {"waiting": {"reason": reason, "message": message}}}


class ProbeApi(K8sApi):
    """A cluster whose probe pod shows `statuses` and `events`."""

    def __init__(self, statuses=None, events=None, log=""):
        self.statuses = statuses or []
        self.events = events or []
        self.log = log
        self.applied: list[dict] = []
        self.deleted: list[tuple[str, str]] = []

    def apply(self, manifest):
        self.applied.append(manifest)

    def get(self, resource, name):
        return None

    def delete(self, resource, name):
        self.deleted.append((resource, name))

    def list(self, resource, label_selector=""):
        if resource == "nodes":
            return [_node()]
        if resource == "pods":
            assert label_selector.startswith(PREFLIGHT_LABEL)
            return [{
                "metadata": {"name": "probe-pod"},
                "spec": {"nodeName": "gpu-node-1"},
                "status": {"containerStatuses": self.statuses},
            }]
        if resource == "events":
            return [{"involvedObject": {"kind": "Pod", "name": "probe-pod"}, **e}
                    for e in self.events]
        return []

    def logs(self, label_selector, tail=200):
        return self.log


def _settings(**over):
    return get_settings().model_copy(update={
        "k8s_api_mode": "client", "k8s_namespace": "runs", **over})


def _machine() -> MachineInfo:
    return MachineInfo(name="gpu-node-1", host="k8s", gpu_count=8, driver="k8s",
                       node_selector="kubernetes.io/hostname=gpu-node-1")


def _run(api: ProbeApi, timeout: float = 5, **settings) -> dict:
    driver = K8sDriver(api=api, settings=_settings(**settings))
    checks = driver.preflight(_machine(), image=ENGINE, model_path=MODEL,
                              policy_images=[POLICY], widest_candidate_cards=2,
                              timeout=timeout)
    return {c.key: c for c in checks}


def test_everything_present_passes_and_cleans_up():
    api = ProbeApi([_terminated("engine"), _terminated("policy-0")],
                   [{"reason": "Pulled", "type": "Normal"}])
    checks = _run(api)
    assert checks["node"].status == "pass"
    assert checks["gpus"].status == "pass"
    assert checks["image"].status == "pass"
    assert checks[f"policy_image:{POLICY}"].status == "pass"
    assert checks["model_path"].status == "pass"
    assert api.deleted and api.deleted[0][0] == "job"


def test_an_image_the_node_cannot_pull_fails():
    api = ProbeApi([_waiting("engine", "ErrImagePull", "manifest unknown"),
                    _terminated("policy-0")])
    checks = _run(api)
    assert checks["image"].status == "fail"
    assert "manifest unknown" in checks["image"].detail
    assert checks[f"policy_image:{POLICY}"].status == "pass"
    assert api.deleted


def test_a_missing_model_fails_the_mount():
    api = ProbeApi(
        [_waiting("engine", "ContainerCreating"), _waiting("policy-0", "ContainerCreating")],
        [{"reason": "FailedMount", "type": "Warning",
          "message": "hostPath type check failed: /mnt/models/qwen is not a directory"}],
    )
    checks = _run(api)
    assert checks["model_path"].status == "fail"
    assert "not a directory" in checks["model_path"].detail
    assert checks["image"].status == "skip"
    assert api.deleted


def test_a_slow_pull_warns_and_is_left_to_finish():
    api = ProbeApi(
        [_waiting("engine", "ContainerCreating"), _terminated("policy-0")],
        [{"reason": "Pulling", "type": "Normal"}],
    )
    checks = _run(api, timeout=0)
    assert checks["image"].status == "warn"
    # The pull started, so the mount had already worked.
    assert checks["model_path"].status == "pass"
    assert not api.deleted


def test_a_weights_claim_is_checked_from_inside():
    api = ProbeApi([_terminated("engine"), _terminated("policy-0")], log="model-missing\n")
    checks = _run(api, k8s_model_pvc="weights", k8s_model_pvc_root="/mnt/models")
    assert checks["model_path"].status == "fail"
    pod = api.applied[0]["spec"]["template"]["spec"]
    assert pod["volumes"][0]["persistentVolumeClaim"]["claimName"] == "weights"
    assert "/weights/qwen" in pod["containers"][0]["command"][2]


def test_the_probe_runs_where_a_run_would():
    job, reads_log = render_preflight_job(
        "autotune-preflight-x", _machine(),
        [("engine", "", "image", ENGINE), ("policy-0", "", "p", POLICY)],
        MODEL, _settings())
    pod = job["spec"]["template"]["spec"]
    assert not reads_log
    assert pod["nodeSelector"] == {"kubernetes.io/hostname": "gpu-node-1"}
    assert any(t["key"] == "nvidia.com/gpu" for t in pod["tolerations"])
    assert pod["volumes"] == [{"name": "model",
                               "hostPath": {"path": MODEL, "type": "Directory"}}]
    engine, policy = pod["containers"]
    assert engine["image"] == ENGINE and "volumeMounts" in engine
    assert policy["image"] == POLICY and "volumeMounts" not in policy
    assert {"name": "NVIDIA_VISIBLE_DEVICES", "value": "void"} in engine["env"]
    assert "nvidia.com/gpu" not in str(engine["resources"])
    assert job["spec"]["ttlSecondsAfterFinished"] > 0
