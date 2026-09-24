"""The k8s driver's logic, over a fake cluster transport.

The whole point of the K8sApi seam is that this — render workload objects, map
their status onto a DeploymentState, find them again, tear them down — is
testable before a real cluster. The fake is an in-memory store keyed the way
kubectl addresses objects: (resource, name). A live-cluster counterpart lives in
test_k8s_integration.py (auto-skips without a cluster).
"""

from app.control.launch import get_driver
from app.control.launch.base import DeploymentState, LaunchSpec, MachineInfo
from app.control.launch.k8s import RUN_LABEL, K8sDriver, _gpu_count, render_workload
from app.control.launch.k8s_api import K8sApi, UnavailableK8sApi
from app.core.config import Settings, get_settings


class FakeK8sApi(K8sApi):
    def __init__(self, settings: Settings):
        self.settings = settings
        self.objects: dict[tuple[str, str], dict] = {}
        self.pods: list[dict] = []
        self.nodes: list[dict] = []
        self.events: list[dict] = []
        self.applied: list[dict] = []
        self.deleted: list[tuple[str, str]] = []

    def _resource_of(self, manifest: dict) -> str:
        kind = manifest.get("kind", "")
        if kind == "Deployment":
            return "deployment"
        if kind == "Service":
            return "service"
        if kind == "Job":
            return "job"
        return f"{self.settings.k8s_cr_plural}.{self.settings.k8s_cr_group}"

    def apply(self, manifest):
        items = manifest if isinstance(manifest, list) else [manifest]
        for m in items:
            self.applied.append(m)
            self.objects[(self._resource_of(m), m["metadata"]["name"])] = m

    def get(self, resource, name):
        return self.objects.get((resource, name))

    def delete(self, resource, name):
        self.deleted.append((resource, name))
        self.objects.pop((resource, name), None)

    def list(self, resource, label_selector=""):
        if resource == "pods":
            return list(self.pods)
        if resource == "nodes":
            return list(self.nodes)
        if resource == "events":
            return list(self.events)
        return []

    def logs(self, label_selector, tail=200):
        return f"logs for {label_selector}"


def _clone(**over) -> Settings:
    # Settings is a pydantic model; model_copy keeps it a real Settings so the
    # driver reads k8s_* fields off it.
    return get_settings().model_copy(update=over)


def _spec(**over):
    base = dict(
        run_id=7,
        machine=MachineInfo(
            name="pool-a", host="svc.ns.svc.cluster.local", gpu_count=8, driver="k8s"
        ),
        engine="sglang",
        image="sglang:latest",
        model_path="/mnt/disk0/models/qwen",
        served_model_name="glm-5",
        engine_args={"tp": 4},
        gpu_indices=[0, 1, 2, 3],
        port=28200,
    )
    base.update(over)
    return LaunchSpec(**base)


def _driver(settings, api=None):
    api = api if api is not None else FakeK8sApi(settings)
    return K8sDriver(api=api, settings=settings)


# -- render: deployment mode (the default, generic single-node path) ----------


def test_default_mode_renders_a_deployment_and_a_nodeport_service():
    settings = _clone(k8s_workload_kind="deployment")
    objs = render_workload(_spec(), settings)
    kinds = [o["kind"] for o in objs]
    assert kinds == ["Deployment", "Service"]

    deployment, service = objs
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    # GPU-ness is per machine: a machine with cards gets the resource + runtimeClass.
    assert container["resources"]["limits"][settings.k8s_gpu_resource] == 4
    assert deployment["spec"]["template"]["spec"]["runtimeClassName"] == "nvidia"
    # The engine launch is wrapped in a one-line guard that fails fast if the
    # node is missing the model hostPath, then execs the SAME argv the ssh driver
    # would build.
    assert container["command"] == ["/bin/sh"]
    guard = container["args"][1]
    assert container["args"][0] == "-c"
    assert "no model weights at /model" in guard and "exit 66" in guard
    assert "exec python3 -m sglang.launch_server" in guard
    assert "--tp" in guard
    # Readiness is the k8s-native /v1/models probe (no worker-side HTTP needed).
    assert container["readinessProbe"]["httpGet"]["path"] == "/v1/models"
    # Deterministic name → attach/teardown find it after a restart.
    assert deployment["metadata"]["name"] == "autotune-run-7"
    assert service["spec"]["type"] == "NodePort"


def test_a_gpuless_machine_gets_no_gpu_request_or_runtimeclass():
    # This is what lets the driver run on a GPU-less test cluster, and mirrors
    # the ssh driver's per-machine GPU-ness.
    settings = _clone(k8s_workload_kind="deployment")
    spec = _spec(gpu_indices=[], machine=MachineInfo(name="cpu", host="h", gpu_count=0))
    deployment = render_workload(spec, settings)[0]
    pod = deployment["spec"]["template"]["spec"]
    assert "runtimeClassName" not in pod
    assert "resources" not in pod["containers"][0]
    assert all(v["name"] != "dshm" for v in pod["volumes"])  # no shm on a CPU pod


def test_deployment_gpu_pod_gets_a_safe_dev_shm():
    # Parity with the ssh driver's `docker run --ipc=host`: a GPU pod gets a
    # sized in-memory /dev/shm (emptyDir Memory) — not hostIPC.
    settings = _clone(k8s_workload_kind="deployment", k8s_shm_size_mb=4096)
    pod = render_workload(_spec(), settings)[0]["spec"]["template"]["spec"]
    dshm = next((v for v in pod["volumes"] if v["name"] == "dshm"), None)
    assert dshm["emptyDir"] == {"medium": "Memory", "sizeLimit": "4096Mi"}
    mount = next(m for m in pod["containers"][0]["volumeMounts"] if m["name"] == "dshm")
    assert mount["mountPath"] == "/dev/shm"


def test_gpu_count_is_a_count_never_device_indices():
    assert _gpu_count(_spec()) == 4  # len(gpu_indices)
    assert _gpu_count(_spec(gpu_indices=[])) == 8  # whole machine


# -- lifecycle over the fake: deployment mode ---------------------------------


def test_launch_applies_both_objects_and_states_track_the_pods():
    settings = _clone(k8s_workload_kind="deployment")
    api = FakeK8sApi(settings)
    driver = _driver(settings, api)
    spec = _spec()

    handle, command = driver.launch(spec)
    assert command.startswith("kubectl apply -f -")
    assert [m["kind"] for m in api.applied] == ["Deployment", "Service"]

    # Deployment exists but no pods scheduled yet → still coming up.
    assert driver.state(handle) == DeploymentState.STARTING

    # A pod reporting Ready (kubelet's own /v1/models probe passed) → READY.
    api.pods = [{"status": {"conditions": [{"type": "Ready", "status": "True"}]}}]
    assert driver.state(handle) == DeploymentState.READY

    # A pod stuck pulling / crash-looping → CRASHED, not STARTING forever.
    api.pods = [{"status": {"containerStatuses": [
        {"state": {"waiting": {"reason": "CrashLoopBackOff"}}}
    ]}}]
    assert driver.state(handle) == DeploymentState.CRASHED


def test_teardown_removes_deployment_and_service_and_is_idempotent():
    settings = _clone(k8s_workload_kind="deployment")
    api = FakeK8sApi(settings)
    driver = _driver(settings, api)
    spec = _spec()
    handle, _ = driver.launch(spec)
    assert driver.attach(spec) is not None

    driver.teardown(handle)
    assert ("deployment", "autotune-run-7") in api.deleted
    assert ("service", "autotune-run-7") in api.deleted
    assert driver.attach(spec) is None
    assert driver.state(handle) == DeploymentState.GONE
    driver.teardown(handle)  # again: nothing there, still fine


def test_deployment_endpoint_is_the_nodeport_on_a_node_ip():
    settings = _clone(k8s_workload_kind="deployment")
    api = FakeK8sApi(settings)
    api.nodes = [{"status": {"addresses": [{"type": "InternalIP", "address": "10.0.0.5"}]}}]
    driver = _driver(settings, api)
    handle, _ = driver.launch(_spec())
    # The Service got a NodePort (fake stores what was applied; give it one).
    svc = api.objects[("service", "autotune-run-7")]
    svc["spec"]["ports"][0]["nodePort"] = 31234
    assert driver._deployment_endpoint(_spec()) == "http://10.0.0.5:31234"


def test_deployment_endpoint_falls_back_without_a_node_ip():
    settings = _clone(k8s_workload_kind="deployment", k8s_node_host="")
    api = FakeK8sApi(settings)
    api.nodes = []  # readable, but it names no InternalIP
    driver = _driver(settings, api)
    driver.launch(_spec())
    svc = api.objects[("service", "autotune-run-7")]
    svc["spec"]["ports"][0]["nodePort"] = 31234
    # No node IP and no node_host: the spec's own host:port is the honest answer.
    assert driver._deployment_endpoint(_spec()) == _spec().endpoint_url


def test_endpoint_uses_node_host_when_nodes_are_unreadable():
    """The gpu-cluster-a reality: the scoped SA has no cluster-scoped `nodes` grant.

    Endpoint derivation must then come from the cluster's `node_host` rather
    than an InternalIP, or every run deploys successfully and is never
    reachable. This is why per-cluster `node_host` exists."""
    settings = _clone(k8s_workload_kind="deployment", k8s_node_host="198.51.100.84")
    api = FakeK8sApi(settings)

    def _forbidden(*_args, **_kwargs):
        raise RuntimeError("Forbidden")

    api.list = _forbidden  # every list fails, exactly as it does on B300
    driver = _driver(settings, api)
    driver.launch(_spec())  # apply does not list, so the run still deploys
    svc = api.objects[("service", "autotune-run-7")]
    svc["spec"]["ports"][0]["nodePort"] = 31234
    assert driver._deployment_endpoint(_spec()) == "http://198.51.100.84:31234"


def test_exit_info_reads_a_terminated_pod():
    settings = _clone(k8s_workload_kind="deployment")
    api = FakeK8sApi(settings)
    api.pods = [{"status": {"containerStatuses": [
        {"state": {"terminated": {"exitCode": 137, "reason": "OOMKilled"}}}
    ]}}]
    driver = _driver(settings, api)
    handle, _ = driver.launch(_spec())
    assert driver.exit_info(handle) == (137, True)


# -- custom mode: a TuningRun for the autotune-operator -----------------------


def _cr_key(settings):
    return f"{settings.k8s_cr_plural}.{settings.k8s_cr_group}"


def test_custom_mode_renders_a_tuningrun():
    settings = _clone(k8s_workload_kind="custom")
    objs = render_workload(_spec(), settings)
    assert [o["kind"] for o in objs] == ["TuningRun"]
    cr = objs[0]
    assert cr["apiVersion"] == "tuning.llm-autotune.io/v1alpha1"
    assert cr["metadata"]["name"] == "autotune-run-7"

    crspec = cr["spec"]
    # Engine-agnostic: the SAME argv the ssh/deployment paths build, wrapped in a
    # missing-weights guard that execs the engine — the operator passes it through.
    assert crspec["command"] == ["/bin/sh"]
    guard = crspec["args"][1]
    assert crspec["args"][0] == "-c"
    assert "no model weights at /model" in guard and "exit 66" in guard
    assert "exec python3 -m sglang.launch_server" in guard
    assert "--tp" in guard
    # The structured fields the operator reads to build pod + Service + probe.
    assert crspec["gpuCount"] == 4  # a count, never device indices
    assert crspec["port"] == 28200
    assert crspec["modelHostPath"] == "/mnt/disk0/models/qwen"
    assert crspec["modelMountPath"] == "/model"  # MODEL_MOUNT, matches the argv
    assert crspec["serviceType"] == "NodePort"
    assert "ttlSeconds" not in crspec  # default 0 → omitted


def test_the_engine_is_guarded_against_a_node_missing_the_model():
    """The scheduler can place a pod on a node whose per-node hostPath lacks the
    model. The guard checks the mount is non-empty and fails in ~1s if not,
    instead of letting the engine start and spend a minute finding no weights —
    then execs the real engine so PID 1 / signals are unchanged."""
    from app.control.launch.k8s import _guarded_command

    wrapped = _guarded_command(["python3", "-m", "sglang.launch_server", "--model-path", "/model"])
    assert wrapped[:2] == ["/bin/sh", "-c"]
    guard = wrapped[2]
    assert "ls -A /model" in guard  # the presence check
    assert "exit 66" in guard        # fails fast, distinctive code
    assert guard.rstrip().endswith("exec python3 -m sglang.launch_server --model-path /model")


def test_custom_mode_writes_ttl_when_configured():
    settings = _clone(k8s_workload_kind="custom", k8s_run_ttl_seconds=1800)
    cr = render_workload(_spec(), settings)[0]
    assert cr["spec"]["ttlSeconds"] == 1800


def test_custom_mode_sets_shared_memory_for_gpu_pods():
    # The operator turns sharedMemoryMB into the /dev/shm emptyDir; the driver
    # just asks for it on GPU pods (the safe --ipc=host substitute).
    settings = _clone(k8s_workload_kind="custom", k8s_shm_size_mb=4096)
    assert render_workload(_spec(), settings)[0]["spec"]["sharedMemoryMB"] == 4096
    cpu = render_workload(
        _spec(gpu_indices=[], machine=MachineInfo(name="c", host="h", gpu_count=0)), settings
    )[0]
    assert "sharedMemoryMB" not in cpu["spec"]


def test_custom_mode_state_follows_the_operator_phase():
    # We trust the operator's phase (it gates on the pod's own /v1/models probe)
    # instead of probing the endpoint ourselves.
    settings = _clone(k8s_workload_kind="custom")
    api = FakeK8sApi(settings)
    driver = _driver(settings, api)
    handle, _ = driver.launch(_spec())

    # No status yet → still coming up.
    assert driver.state(handle) == DeploymentState.STARTING

    obj = api.objects[(_cr_key(settings), "autotune-run-7")]
    obj["status"] = {"phase": "Ready"}
    assert driver.state(handle) == DeploymentState.READY
    for dead in ("Failed", "Expired"):
        obj["status"] = {"phase": dead}
        assert driver.state(handle) == DeploymentState.CRASHED


def test_unschedulable_reads_as_coming_up_but_a_hard_failure_still_crashes():
    """An unschedulable pod is queued, not wedged: it schedules the moment a node
    frees the GPUs it asked for. Between our own rounds the previous pod is still
    releasing its cards, so the next one is briefly unschedulable — failing it
    there turned a few-second wait into a spurious 'unschedulable' run. So it
    reads as STARTING; a genuine hard failure (bad image) still CRASHES fast."""
    settings = _clone(k8s_workload_kind="custom")
    api = FakeK8sApi(settings)
    driver = _driver(settings, api)
    handle, _ = driver.launch(_spec())

    api.pods = [{"status": {"conditions": [
        {"type": "PodScheduled", "status": "False", "reason": "Unschedulable",
         "message": "0/16 nodes are available: 1 Insufficient nvidia.com/gpu"}
    ]}}]
    assert driver.state(handle) == DeploymentState.STARTING

    api.pods = [{"status": {"containerStatuses": [
        {"state": {"waiting": {"reason": "ImagePullBackOff", "message": "no such image"}}}
    ]}}]
    assert driver.state(handle) == DeploymentState.CRASHED


def test_custom_mode_endpoint_comes_from_operator_status():
    settings = _clone(k8s_workload_kind="custom")
    api = FakeK8sApi(settings)
    driver = _driver(settings, api)
    spec = _spec()
    driver.launch(spec)

    # Before the operator reports one, attach falls back to the spec host:port.
    assert driver.attach(spec).endpoint_url == spec.endpoint_url
    # Once the operator publishes the reachable URL, attach adopts it — this is
    # what the supervisor stores when the run goes READY.
    api.objects[(_cr_key(settings), "autotune-run-7")]["status"] = {
        "phase": "Ready",
        "endpoint": "http://10.0.0.9:31888",
    }
    assert driver.attach(spec).endpoint_url == "http://10.0.0.9:31888"


def test_custom_mode_finds_pods_by_the_operator_label():
    # Regression (caught driving the live operator): in custom mode the operator
    # owns the pods and labels them tuning.llm-autotune.io/run=<cr name>, so logs /
    # environment / exit-info must select on that label — not the run-id label
    # the driver only puts on objects it creates itself in deployment mode.
    spec = _spec()
    custom = _driver(_clone(k8s_workload_kind="custom"))
    assert custom._run_selector(spec) == "tuning.llm-autotune.io/run=autotune-run-7"
    deployment = _driver(_clone(k8s_workload_kind="deployment"))
    assert deployment._run_selector(spec) == f"{RUN_LABEL}=7"


# -- placement: nodeSelector (weights are a per-node hostPath) ----------------


def _k8s_machine(**over):
    base = dict(name="pool", host="k8s", gpu_count=8, driver="k8s")
    base.update(over)
    return MachineInfo(**base)


def test_node_selector_comes_from_the_machine_slice():
    # A k8s machine IS a named node-slice, so its selector is a property of the
    # machine (set on the Resources page), rendered in both workload modes. Two
    # pairs, tolerant of spaces.
    sel = "kubernetes.io/hostname=gpu-a100-1, nvidia.com/gpu.product=A100"
    want = {"kubernetes.io/hostname": "gpu-a100-1", "nvidia.com/gpu.product": "A100"}
    spec = _spec(machine=_k8s_machine(node_selector=sel))

    dep = render_workload(spec, _clone(k8s_workload_kind="deployment"))[0]
    assert dep["spec"]["template"]["spec"]["nodeSelector"] == want
    cr = render_workload(spec, _clone(k8s_workload_kind="custom"))[0]
    assert cr["spec"]["nodeSelector"] == want


def test_node_selector_machine_wins_over_global_then_falls_back():
    # The machine's slice takes precedence; the global AUTOTUNE_K8S_NODE_SELECTOR
    # is only a fallback for a machine that declares none.
    pinned = _spec(machine=_k8s_machine(node_selector="kubernetes.io/hostname=gpu-h100-2"))
    won = render_workload(
        pinned, _clone(k8s_workload_kind="custom", k8s_node_selector="kubernetes.io/hostname=OTHER")
    )[0]
    assert won["spec"]["nodeSelector"] == {"kubernetes.io/hostname": "gpu-h100-2"}

    # No machine selector → the global fills in.
    plain = _spec(machine=_k8s_machine())
    fell_back = render_workload(
        plain, _clone(k8s_workload_kind="custom", k8s_node_selector="kubernetes.io/hostname=global")
    )[0]
    assert fell_back["spec"]["nodeSelector"] == {"kubernetes.io/hostname": "global"}

    # Neither → the scheduler is free (no key emitted at all).
    bare = render_workload(plain, _clone(k8s_workload_kind="custom"))[0]
    assert "nodeSelector" not in bare["spec"]


# -- failure reasons: a run that never started still says why -----------------


def test_failure_reason_from_a_failed_mount_event():
    # The real bug that stalled a run: a hostPath model missing from the node.
    # FailedMount is a pod *event*, never a container waiting reason, so it only
    # surfaces by reading events.
    settings = _clone(k8s_workload_kind="custom")
    api = FakeK8sApi(settings)
    api.pods = [{"metadata": {"name": "autotune-run-7-abc"},
                 "status": {"containerStatuses": [
                     {"state": {"waiting": {"reason": "ContainerCreating"}}}]}}]
    api.events = [{
        "type": "Warning", "reason": "FailedMount", "lastTimestamp": "2026-08-11T04:00:00Z",
        "involvedObject": {"kind": "Pod", "name": "autotune-run-7-abc"},
        "message": "/mnt/disk0/models/Qwen3.5-4B is not a directory",
    }]
    driver = _driver(settings, api)
    handle, _ = driver.launch(_spec())
    cls, msg = driver.failure_reason(handle)
    assert cls == "mount_failed"
    assert "not a directory" in msg
    # And a wedged pod fails fast even while the operator still says "starting".
    api.objects[(_cr_key(settings), "autotune-run-7")]["status"] = {"phase": "Starting"}
    assert driver.state(handle) == DeploymentState.CRASHED


def test_failure_reason_from_waiting_and_unschedulable():
    settings = _clone(k8s_workload_kind="custom")
    api = FakeK8sApi(settings)
    driver = _driver(settings, api)
    handle, _ = driver.launch(_spec())

    api.pods = [{"metadata": {"name": "p"}, "status": {"containerStatuses": [
        {"state": {"waiting": {"reason": "ImagePullBackOff", "message": "no such image"}}}]}}]
    assert driver.failure_reason(handle) == ("image_pull", "no such image")

    api.pods = [{"metadata": {"name": "p"}, "status": {"conditions": [
        {"type": "PodScheduled", "status": "False", "reason": "Unschedulable",
         "message": "0/7 nodes are available"}]}}]
    assert driver.failure_reason(handle) == ("unschedulable", "0/7 nodes are available")


def test_failure_reason_is_none_when_pod_is_healthy():
    settings = _clone(k8s_workload_kind="custom")
    api = FakeK8sApi(settings)
    api.pods = [{"metadata": {"name": "p"}, "status": {
        "conditions": [{"type": "Ready", "status": "True"}]}}]
    driver = _driver(settings, api)
    handle, _ = driver.launch(_spec())
    assert driver.failure_reason(handle) is None


# -- substrate-neutral bits ---------------------------------------------------


def test_default_transport_is_unavailable_until_configured():
    driver = get_driver("k8s")
    assert isinstance(driver.api, UnavailableK8sApi)


def test_baseline_lifecycle_is_a_cluster_managed_noop():
    driver = _driver(_clone(k8s_workload_kind="deployment"))
    machine = MachineInfo(name="pool-a", host="", gpu_count=8, driver="k8s")
    captured = driver.capture_baseline(machine)
    assert captured["services"] == []
    assert driver.clear_baseline(machine, captured) == []
    assert driver.restore_baseline(machine, captured) == []


# -- tolerations -------------------------------------------------------------
#
# A GPU node is commonly tainted so that pods with no use for cards keep off
# it. The pod that ASKS for cards is exactly what the taint protects the node
# for — every GPU workload on such a cluster carries the matching toleration — and
# a platform that does not say so sits Pending on a pool with eight free GPUs,
# reporting only "untolerated taint".


def _pod_of(settings, spec=None):
    return render_workload(spec or _spec(), settings)[0]["spec"]["template"]["spec"]


def test_a_gpu_pod_tolerates_the_gpu_taint():
    pod = _pod_of(_clone(k8s_workload_kind="deployment"))
    assert pod["tolerations"] == [
        {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}
    ]


def test_a_pod_with_no_cards_does_not():
    """The toleration follows the GPU request, not the driver: a card-less pod
    has no business on a node reserved for cards."""
    settings = _clone(k8s_workload_kind="deployment")
    spec = _spec(machine=MachineInfo(name="cpu-pool", host="h", gpu_count=0, driver="k8s"),
                 gpu_indices=[])
    assert "tolerations" not in _pod_of(settings, spec)


def test_further_taints_are_declared_explicitly():
    pod = _pod_of(
        _clone(
            k8s_workload_kind="deployment",
            k8s_tolerations="dedicated=ml:NoSchedule, spot:NoExecute, weird",
        )
    )
    assert pod["tolerations"][1:] == [
        {"key": "dedicated", "operator": "Equal", "value": "ml", "effect": "NoSchedule"},
        {"key": "spot", "operator": "Exists", "effect": "NoExecute"},
        {"key": "weird", "operator": "Exists"},
    ]


def test_a_cordoned_pool_is_usable_via_an_explicit_toleration():
    """Some GPU pools are cordoned ON PURPOSE (reserved for the tuning platform).
    Cordoning taints the node `node.kubernetes.io/unschedulable:NoSchedule`, and
    a pod that tolerates that taint still schedules onto it — verified live on
    a tainted GPU node, which stays Pending without this and runs once it is added.
    So `k8s_tolerations` must be able to carry it; otherwise a run waits forever
    on a node the scheduler calls "unschedulable" and it reads as a platform bug
    rather than the deliberate policy it is."""
    pod = _pod_of(
        _clone(
            k8s_workload_kind="deployment",
            k8s_tolerations="node.kubernetes.io/unschedulable",
        )
    )
    assert pod["tolerations"] == [
        {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"},
        {"key": "node.kubernetes.io/unschedulable", "operator": "Exists"},
    ]
    # An explicit toleration is not tied to the GPU request, so a card-less pod
    # (a policy job) carries it too.
    cardless = _spec(
        machine=MachineInfo(name="cpu-pool", host="h", gpu_count=0, driver="k8s"),
        gpu_indices=[],
    )
    assert _pod_of(
        _clone(
            k8s_workload_kind="deployment",
            k8s_tolerations="node.kubernetes.io/unschedulable",
        ),
        cardless,
    )["tolerations"] == [
        {"key": "node.kubernetes.io/unschedulable", "operator": "Exists"}
    ]


def test_the_gpu_toleration_can_be_turned_off():
    pod = _pod_of(_clone(k8s_workload_kind="deployment", k8s_tolerate_gpu_taint=False))
    assert "tolerations" not in pod


def test_custom_mode_carries_them_too():
    """TuningRun carries tolerations too. A CRD without the field has the API
    server prune them, and the pod sits Pending on a tainted pool naming
    nothing."""
    cr = render_workload(_spec(), _clone(k8s_workload_kind="custom"))[0]
    assert cr["spec"]["tolerations"] == [
        {"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}
    ]


# -- registry credentials ----------------------------------------------------
#
# Nodes already carrying registry credentials mean nothing we ran needed a pull secret.
# A cluster whose nodes do not fails every launch with ImagePullBackOff, and
# the fix is not code — it is this setting, in all three renderers.


def test_no_pull_secret_renders_no_field():
    """Not an empty list: a field we do not need should not appear at all."""
    assert "imagePullSecrets" not in _pod_of(_clone(k8s_workload_kind="deployment"))


def test_pull_secrets_reach_every_pod_shape():
    settings = _clone(k8s_workload_kind="deployment", k8s_image_pull_secrets="registry-cred, other")
    assert _pod_of(settings)["imagePullSecrets"] == [
        {"name": "registry-cred"},
        {"name": "other"},
    ]
    cr = render_workload(_spec(), _clone(k8s_workload_kind="custom",
                                         k8s_image_pull_secrets="registry-cred"))[0]
    # the CRD takes bare names, not LocalObjectReferences
    assert cr["spec"]["imagePullSecrets"] == ["registry-cred"]


# -- where the weights come from ---------------------------------------------
#
# A hostPath makes the weights a property of the NODE: a run can only go where
# someone staged the model, which is what pins a campaign to one machine and
# what turned one dead GPU into nine dead runs. A shared claim removes that.


def test_weights_are_a_node_hostpath_by_default():
    pod = _pod_of(_clone(k8s_workload_kind="deployment"))
    assert pod["volumes"][0]["hostPath"] == {"path": "/mnt/disk0/models/qwen"}
    assert "subPath" not in pod["containers"][0]["volumeMounts"][0]


def test_a_shared_claim_mounts_only_this_run_s_directory():
    settings = _clone(
        k8s_workload_kind="deployment",
        k8s_model_pvc="model-weights",
        k8s_model_pvc_root="/mnt/disk0/models",
    )
    pod = _pod_of(settings)
    assert pod["volumes"][0]["persistentVolumeClaim"] == {"claimName": "model-weights"}
    assert "hostPath" not in pod["volumes"][0]
    mount = pod["containers"][0]["volumeMounts"][0]
    assert mount["mountPath"] == "/model" and mount["subPath"] == "qwen"

    cr = render_workload(_spec(), _clone(k8s_workload_kind="custom",
                                         k8s_model_pvc="model-weights",
                                         k8s_model_pvc_root="/mnt/disk0/models"))[0]
    assert cr["spec"]["modelPVC"] == "model-weights"
    assert cr["spec"]["modelSubPath"] == "qwen"
    assert "modelHostPath" not in cr["spec"]


def test_a_model_outside_the_declared_root_is_left_visibly_wrong():
    """Rewriting it to something plausible would mount the wrong weights and
    still benchmark. A path that reads oddly in the manifest is the cheaper
    failure."""
    settings = _clone(
        k8s_workload_kind="deployment",
        k8s_model_pvc="model-weights",
        k8s_model_pvc_root="/srv/elsewhere",
    )
    mount = _pod_of(settings)["containers"][0]["volumeMounts"][0]
    assert mount["subPath"] == "mnt/disk0/models/qwen"


def test_extra_volumes_stay_paired_with_their_mounts():
    """Volumes and mounts used to be named from two independent sorts. With one
    model mount the orders agreed; a second volume whose host and container
    paths sort differently is where that mispairs."""
    spec = _spec(volumes={"/zz/host-cache": "/aa/container-cache"})
    pod = _pod_of(_clone(k8s_workload_kind="deployment"), spec)
    by_name = {v["name"]: v["hostPath"]["path"] for v in pod["volumes"] if "hostPath" in v}
    mounts = {m["name"]: m["mountPath"] for m in pod["containers"][0]["volumeMounts"]}
    assert by_name["vol-0"] == "/mnt/disk0/models/qwen" and mounts["vol-0"] == "/model"
    assert by_name["vol-1"] == "/zz/host-cache" and mounts["vol-1"] == "/aa/container-cache"
