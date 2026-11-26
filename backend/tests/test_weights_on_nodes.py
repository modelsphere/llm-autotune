"""Where the weights are: checked node by node, and runs kept to those nodes.

A k8s machine can be a pool of nodes whose weights (a per-node hostPath) live
on only some of them; placement picked any node and the run died loading the
model. Drivers now say, per node, whether the weights path is there, and a
campaign's `node_names` keeps its runs to the nodes that have it."""

from app.control.launch.base import LaunchSpec, MachineInfo
from app.control.launch.k8s import HOSTNAME_LABEL, K8sDriver, render_workload
from app.control.launch.k8s_api import K8sApi
from app.control.launch.ssh_docker import SshDockerDriver
from app.core.config import get_settings

ENGINE = "registry.example.com/sglang:v1"
MODEL = "/mnt/models/qwen"


def _settings(**over):
    return get_settings().model_copy(update={
        "k8s_api_mode": "client", "k8s_namespace": "runs",
        "k8s_workload_kind": "deployment", **over})


def _node(name: str, ready: bool = True) -> dict:
    return {
        "metadata": {"name": name, "labels": {HOSTNAME_LABEL: name}},
        "spec": {},
        "status": {"allocatable": {"nvidia.com/gpu": "8"},
                   "conditions": [{"type": "Ready", "status": "True" if ready else "False"}]},
    }


POOL = MachineInfo(name="gpu-pool", host="k8s", gpu_count=8, driver="k8s",
                   node_selector="nvidia.com/gpu.product=H800")


class PoolApi(K8sApi):
    """Three nodes; the weights are on `have`. Each probe pod lands on the node
    its job pins, and mounts or fails there."""

    def __init__(self, have: set[str]):
        self.have = have
        self.jobs: dict[str, str] = {}  # job name -> node
        self.deleted: list[str] = []

    def apply(self, manifest):
        selector = manifest["spec"]["template"]["spec"]["nodeSelector"]
        self.jobs[manifest["metadata"]["name"]] = selector[HOSTNAME_LABEL]

    def get(self, resource, name):
        return None

    def delete(self, resource, name):
        self.deleted.append(name)

    def list(self, resource, label_selector=""):
        if resource == "nodes":
            return [_node("gpu-node-a"), _node("gpu-node-b"), _node("gpu-node-c"),
                    _node("gpu-node-d", ready=False)]
        if resource == "pods":
            job = label_selector.split("=", 1)[1]
            node = self.jobs[job]
            status = ({"state": {"terminated": {"exitCode": 0}}} if node in self.have
                      else {"state": {"waiting": {"reason": "ContainerCreating"}}})
            return [{"metadata": {"name": f"pod-{job}"}, "spec": {"nodeName": node},
                     "status": {"containerStatuses": [{"name": "engine", **status}]}}]
        if resource == "events":
            return [{"involvedObject": {"kind": "Pod", "name": f"pod-{job}"},
                     "reason": "FailedMount", "type": "Warning",
                     "message": f"hostPath type check failed: {MODEL} is not a directory"}
                    for job, node in self.jobs.items() if node not in self.have] + [
                    {"involvedObject": {"kind": "Pod", "name": f"pod-{job}"},
                     "reason": "Pulled", "type": "Normal"}
                    for job, node in self.jobs.items() if node in self.have]
        return []

    def logs(self, label_selector, tail=200):
        return ""


def test_a_pool_reports_each_schedulable_node_and_cleans_up():
    api = PoolApi(have={"gpu-node-a", "gpu-node-c"})
    driver = K8sDriver(api=api, settings=_settings())
    found = driver.model_presence(POOL, image=ENGINE, model_path=MODEL, timeout=5)
    assert {r["node"]: r["present"] for r in found} == {
        "gpu-node-a": True, "gpu-node-b": False, "gpu-node-c": True}
    assert "not a directory" in next(r for r in found if r["node"] == "gpu-node-b")["detail"]
    assert sorted(api.deleted) == sorted(api.jobs)


def test_a_weights_claim_is_probed_once_for_every_node():
    class ClaimApi(PoolApi):
        def logs(self, label_selector, tail=200):
            return "model-present"

    api = ClaimApi(have={"gpu-node-a", "gpu-node-b", "gpu-node-c"})
    driver = K8sDriver(api=api, settings=_settings(k8s_model_pvc="weights"))
    found = driver.model_presence(POOL, image=ENGINE, model_path=MODEL, timeout=5)
    assert len(api.jobs) == 1
    assert [r["present"] for r in found] == [True, True, True]


def _spec(nodes: list[str]) -> LaunchSpec:
    return LaunchSpec(run_id=7, machine=POOL, engine="sglang", image=ENGINE,
                      model_path=MODEL, served_model_name="q", gpu_indices=[0, 1],
                      allowed_nodes=nodes)


def _pod_spec(nodes: list[str]) -> dict:
    deployment = next(m for m in render_workload(_spec(nodes), _settings())
                      if m["kind"] == "Deployment")
    return deployment["spec"]["template"]["spec"]


def test_one_allowed_node_is_pinned_and_several_become_an_affinity():
    assert _pod_spec([])["nodeSelector"] == {"nvidia.com/gpu.product": "H800"}
    assert "affinity" not in _pod_spec([])
    one = _pod_spec(["gpu-node-c"])
    assert one["nodeSelector"][HOSTNAME_LABEL] == "gpu-node-c"
    two = _pod_spec(["gpu-node-a", "gpu-node-c"])
    expr = two["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][
        "nodeSelectorTerms"][0]["matchExpressions"][0]
    assert expr == {"key": HOSTNAME_LABEL, "operator": "In", "values": ["gpu-node-a", "gpu-node-c"]}


def test_allowed_nodes_outside_the_machine_can_never_place():
    api = PoolApi(have=set())
    driver = K8sDriver(api=api, settings=_settings())
    from app.control.launch.base import DeploymentHandle

    handle = DeploymentHandle(driver="k8s", container_name="r", machine=POOL, endpoint_url="")
    pod = {"spec": {"nodeSelector": {HOSTNAME_LABEL: "other-node"}}}
    verdict, why = driver._placement_verdict(handle, pod, "0/4 nodes are available")
    assert verdict == "unplaceable" and "other-node" in why


def test_an_ssh_box_answers_for_itself():
    class _R:
        def __init__(self, out, code=0):
            self.stdout, self.returncode, self.stderr = out, code, ""

    driver = SshDockerDriver()
    box = MachineInfo(name="nv-1", host="10.0.0.1")
    driver._ssh = lambda m, cmd, timeout=60: _R("present")
    assert driver.model_presence(box, image=ENGINE, model_path=MODEL)[0]["present"] is True
    driver._ssh = lambda m, cmd, timeout=60: _R("missing")
    assert driver.model_presence(box, image=ENGINE, model_path=MODEL)[0]["present"] is False
    driver._ssh = lambda m, cmd, timeout=60: _R("", 255)
    assert driver.model_presence(box, image=ENGINE, model_path=MODEL)[0]["present"] is None



def test_a_campaigns_node_names_reach_the_launch():
    from app.db.models import Campaign
    from tests.test_supervisor import make_supervisor

    supervisor, factory = make_supervisor()
    with factory() as s:
        s.get(Campaign, 1).node_names = ["gpu-node-c"]
        s.commit()
    seen = []
    real = supervisor.driver.launch
    supervisor.driver.launch = lambda spec: (seen.append(spec.allowed_nodes), real(spec))[1]
    supervisor.tick()
    assert seen == [["gpu-node-c"]]


def test_each_node_says_how_many_of_the_requested_cards_it_offers():
    # A node can hold the weights and none of the cards a run asks for: the
    # cards of this pool are handed out under two names, one per node.
    class TwoPlugins(PoolApi):
        def list(self, resource, label_selector=""):
            if resource != "nodes":
                return super().list(resource, label_selector)
            a, b = _node("gpu-node-a"), _node("gpu-node-b")
            a["status"]["allocatable"] = {"huawei.com/npu": "8", "huawei.com/Ascend910B4-1": "8"}
            b["status"]["allocatable"] = {"huawei.com/npu": "8", "volcano.sh/Ascend910B4-1": "8"}
            return [a, b]

    from app.control.launch.llmservice import LLMServiceDriver

    settings = _settings(
        k8s_gpu_resource="huawei.com/npu", k8s_gpu_request_resource="volcano.sh/Ascend910B4-1"
    )
    driver = LLMServiceDriver(api=TwoPlugins(have={"gpu-node-a"}), settings=settings)
    found = driver.model_presence(POOL, image=ENGINE, model_path=MODEL, timeout=5)
    assert {r["node"]: (r["present"], r["cards"]) for r in found} == {
        "gpu-node-a": (True, 0), "gpu-node-b": (False, 8)}
