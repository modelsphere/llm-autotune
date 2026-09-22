"""A policy container on the cluster is a Job, not a service.

A TuningRun describes something that SERVES — port, readiness probe, NodePort
Service, weights. A policy has none of those: it dials the platform API, asks
for engine launches and reads results back. Forcing it into the serving CRD
would need a fake port and a readiness probe that can never pass; a Deployment
would resurrect a policy that finished its night cleanly. `batch/v1 Job` is
"run to completion, no restart", which is what `docker run -d` without
`--restart` already means on the ssh path.
"""

from app.control.launch.base import DeploymentHandle, MachineInfo, WorkloadSpec, WorkloadState
from app.control.launch.k8s import POLICY_LABEL, K8sDriver, render_policy_job
from tests.test_k8s_driver import FakeK8sApi, _clone

_MACHINE = MachineInfo(
    name="pool-a", host="", gpu_count=8, driver="k8s",
    node_selector="nvidia.com/gpu.product=NVIDIA-A100-SXM4-80GB",
)


def _spec(**over):
    base = dict(
        name="autotune-policy-5",
        machine=_MACHINE,
        image="registry.example.com/policies/random-search:0.1.0",
        env={"AUTOTUNE_API_URL": "http://192.0.2.10:28100", "AUTOTUNE_SESSION_ID": "5"},
        gpu_indices=None,          # delegated-only: never touches a card
        ports=[28200, 28201],
    )
    base.update(over)
    return WorkloadSpec(**base)


def _driver(**over):
    over.setdefault("k8s_workload_kind", "custom")
    settings = _clone(**over)
    api = FakeK8sApi(settings)
    return K8sDriver(api=api, settings=settings), api


# -- the render ---------------------------------------------------------------


def test_it_is_a_job_that_never_restarts():
    """An exit is final, exactly as on docker. The SDK cannot resume a restarted
    container — its idempotency tags restart at t1 and collide with the previous
    incarnation's — so a retry would be worse than a clean death."""
    job = render_policy_job(_spec(), _clone())
    assert job["apiVersion"] == "batch/v1" and job["kind"] == "Job"
    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["template"]["spec"]["restartPolicy"] == "Never"


def test_a_delegated_policy_asks_for_no_hardware_and_no_placement():
    """The whole reason to run the controller in the cluster: it needs no cards,
    no node selector and no weights, so the scheduler may put it on any node —
    including a CPU one — instead of parking it on contended GPU hardware."""
    pod = render_policy_job(_spec(), _clone())["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert "nodeSelector" not in pod
    assert "runtimeClassName" not in pod
    assert "limits" not in container["resources"]
    assert "volumes" not in pod and "volumeMounts" not in container


def test_it_is_burstable_not_besteffort():
    """A pod with no requests is first to be evicted under node pressure, and
    losing the controller mid-night ends the session."""
    container = render_policy_job(_spec(), _clone())["spec"]["template"]["spec"]["containers"][0]
    assert container["resources"]["requests"] == {"cpu": "100m", "memory": "256Mi"}


def test_the_policy_image_gets_no_cluster_credential():
    """A policy image is user-supplied — that is the point of policy-as-code —
    and must never be handed a token that reaches the API server. Its only
    credential is the scoped AUTOTUNE_API_KEY in its env."""
    pod = render_policy_job(_spec(), _clone())["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False


def test_a_finished_job_outlives_its_post_mortem():
    """The platform reads a dead policy's logs AFTER it exits. A pod reaped in
    minutes would take the only copy of them with it."""
    job = render_policy_job(_spec(), _clone())
    assert job["spec"]["ttlSecondsAfterFinished"] >= 3600


def test_no_hard_deadline_by_default():
    """The platform's own clocks end a session gracefully and let it finalize;
    activeDeadlineSeconds just kills the pod mid-sentence."""
    assert "activeDeadlineSeconds" not in render_policy_job(_spec(), _clone())["spec"]
    job = render_policy_job(_spec(), _clone(k8s_policy_deadline_seconds=172800))
    assert job["spec"]["activeDeadlineSeconds"] == 172800


def test_a_self_serving_policy_takes_cards_placement_and_weights():
    """It runs engines inside its own container, so it is a real GPU workload
    and inherits the machine's placement like an engine would."""
    spec = _spec(gpu_indices=[0, 1], volumes={"/mnt/disk0/models/qwen": "/model:ro"})
    pod = render_policy_job(spec, _clone())["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert container["resources"]["limits"] == {"nvidia.com/gpu": 2}
    assert pod["nodeSelector"] == {"nvidia.com/gpu.product": "NVIDIA-A100-SXM4-80GB"}
    assert pod["runtimeClassName"] == "nvidia"
    assert pod["volumes"][0]["hostPath"]["path"] == "/mnt/disk0/models/qwen"
    assert container["volumeMounts"][0] == {
        "name": "vol-0", "mountPath": "/model", "readOnly": True
    }


def test_env_is_passed_through():
    container = render_policy_job(_spec(), _clone())["spec"]["template"]["spec"]["containers"][0]
    assert {"name": "AUTOTUNE_API_URL", "value": "http://192.0.2.10:28100"} in container["env"]


# -- the driver ---------------------------------------------------------------


def test_launch_workload_applies_the_job_and_marks_the_handle():
    driver, api = _driver()
    handle, command = driver.launch_workload(_spec())
    assert handle.workload is True
    assert handle.endpoint_url == ""  # a workload serves no endpoint we probe
    assert api.applied[0]["kind"] == "Job"
    assert "batch/v1" in command


def test_workload_state_mirrors_docker_inspect():
    driver, api = _driver()
    handle, _ = driver.launch_workload(_spec())
    name = ("job", "autotune-policy-5")

    api.objects[name]["status"] = {"active": 1}
    assert driver.workload_state(handle) == WorkloadState.RUNNING
    api.objects[name]["status"] = {"succeeded": 1}
    assert driver.workload_state(handle) == WorkloadState.EXITED
    api.objects[name]["status"] = {"failed": 1}
    assert driver.workload_state(handle) == WorkloadState.EXITED
    api.objects.pop(name)
    assert driver.workload_state(handle) == WorkloadState.GONE


def test_a_job_with_no_pod_yet_is_running_not_gone():
    """GONE ends the session. A Job waiting on the scheduler has not died."""
    driver, api = _driver()
    handle, _ = driver.launch_workload(_spec())
    assert api.objects[("job", "autotune-policy-5")].get("status") is None
    assert driver.workload_state(handle) == WorkloadState.RUNNING


def test_a_policy_never_selects_an_engines_pods():
    """The trap this label exists for: in deployment mode the engine selector
    parses a run id off the end of the container name, so `autotune-policy-5`
    would read ENGINE run 5's logs."""
    driver, _ = _driver(k8s_workload_kind="deployment")
    policy = DeploymentHandle(
        driver="k8s", container_name="autotune-policy-5", machine=_MACHINE,
        endpoint_url="", workload=True,
    )
    engine = DeploymentHandle(
        driver="k8s", container_name="autotune-run-5", machine=_MACHINE, endpoint_url="",
    )
    assert driver._run_selector(policy) == f"{POLICY_LABEL}=autotune-policy-5"
    assert driver._run_selector(engine) != driver._run_selector(policy)


def test_teardown_deletes_the_job_not_the_engine_object():
    driver, api = _driver()
    handle, _ = driver.launch_workload(_spec())
    driver.teardown(handle)
    assert ("job", "autotune-policy-5") in api.deleted


# -- self-served benchmarks need an address -----------------------------------


def test_a_cluster_machine_refuses_a_self_served_benchmark():
    """POST /benchmarks measures something the POLICY serves itself, from
    `http://{machine.host}:{port}`. On k8s `host` is blank (placement is the
    scheduler's job) and a policy pod has no Service, so the URL would come out
    as `http://:28200` and fail hours later inside LLMBench. Say so up front."""
    from fastapi import HTTPException

    from app.api.policy_sessions import _require_self_serving_is_reachable

    class _M:
        name = "pool-a"
        host = ""

    try:
        _require_self_serving_is_reachable(_M())
    except HTTPException as exc:
        assert exc.status_code == 409
        assert "POST /launches" in exc.detail   # points at the path that works
    else:
        raise AssertionError("a hostless machine must not be accepted")


def test_an_ssh_machine_is_unaffected():
    from app.api.policy_sessions import _require_self_serving_is_reachable

    class _M:
        name = "gpu-01"
        host = "10.0.0.9"

    _require_self_serving_is_reachable(_M())  # no raise
