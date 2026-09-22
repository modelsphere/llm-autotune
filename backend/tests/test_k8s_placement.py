"""Queued, placed, or hopeless — the three answers a cluster can give.

A k8s machine is a slice of a cluster the platform does not own: leasing one
buys the right to ASK the scheduler for cards, never a guarantee of them. So
"the pod has not been placed" is a normal, indefinite state (production is
using the GPUs) and must not be confused with either "the pod is running" or
"the pod can never run".
"""

from app.control.launch.base import MachineInfo
from app.control.launch.k8s import K8sDriver
from tests.test_k8s_driver import FakeK8sApi, _clone, _spec


def _driver(**over):
    settings = _clone(k8s_workload_kind="deployment", **over)
    api = FakeK8sApi(settings)
    driver = K8sDriver(api=api, settings=settings)
    handle, _ = driver.launch(_spec(machine=MachineInfo(
        name="pool-a", host="h", gpu_count=8, driver="k8s",
        node_selector="nvidia.com/gpu.product=NVIDIA-A100-SXM4-80GB",
    )))
    return driver, api, handle


def _node(gpus: int):
    return {
        "metadata": {"name": "gpu-005"},
        "status": {"allocatable": {"nvidia.com/gpu": str(gpus)}},
    }


def _unschedulable_pod(gpus_wanted: int):
    return {
        "metadata": {"name": "autotune-run-7-abc"},
        "spec": {"containers": [
            {"resources": {"limits": {"nvidia.com/gpu": str(gpus_wanted)}}}
        ]},
        "status": {"conditions": [
            {"type": "PodScheduled", "status": "False", "reason": "Unschedulable",
             "message": "0/40 nodes available: insufficient nvidia.com/gpu"},
        ]},
    }


# -- placement(): does this run hold hardware yet? ----------------------------


def test_a_run_with_no_pod_yet_is_queued():
    driver, api, handle = _driver()
    assert driver.placement(handle) == "queued"


def test_an_unbound_pod_is_queued():
    driver, api, handle = _driver()
    api.pods = [_unschedulable_pod(4)]  # no spec.nodeName
    assert driver.placement(handle) == "queued"


def test_a_bound_pod_is_placed_even_while_it_pulls():
    """nodeName is the binding: the cluster has committed these cards to us, so
    the run owns hardware even before a container starts. Withdrawing it would
    throw away real work."""
    driver, api, handle = _driver()
    api.pods = [{"metadata": {"name": "p"}, "spec": {"nodeName": "gpu-005"}, "status": {}}]
    assert driver.placement(handle) == "placed"


def test_an_unreadable_cluster_is_unknown_not_queued():
    """Callers withdraw queued runs. A failed read must never look like an idle
    one, or a transport hiccup would kill live work."""
    driver, api, handle = _driver()

    def _boom(resource, label_selector=""):
        raise RuntimeError("apiserver unreachable")

    api.list = _boom
    assert driver.placement(handle) == "unknown"


# -- the verdict: waiting vs. hopeless ----------------------------------------


def test_a_busy_cluster_is_transient():
    """Nodes exist and are big enough — they are simply full. This is the
    normal state of a cluster shared with production, and waiting is the whole
    answer."""
    driver, api, handle = _driver()
    api.nodes = [_node(8)]
    api.pods = [_unschedulable_pod(4)]
    assert driver.failure_reason(handle) == (
        "unschedulable", "0/40 nodes available: insufficient nvidia.com/gpu"
    )


def test_a_selector_matching_no_node_is_hopeless():
    """A typo'd node selector would otherwise wait politely until the lease
    expired, with nothing to show for the night."""
    driver, api, handle = _driver()
    api.nodes = []
    api.pods = [_unschedulable_pod(4)]
    failure_class, message = driver.failure_reason(handle)
    assert failure_class == "unplaceable"
    assert "no cluster node matches" in message


def test_asking_for_more_cards_than_any_node_has_is_hopeless():
    """tp=8 against a pool of 4-card nodes never schedules, however long we
    wait — and the message carries the arithmetic that proves it."""
    driver, api, handle = _driver()
    api.nodes = [_node(4), _node(4)]
    api.pods = [_unschedulable_pod(8)]
    failure_class, message = driver.failure_reason(handle)
    assert failure_class == "unplaceable"
    assert "8 x nvidia.com/gpu" in message and "has 4" in message


def test_an_unreadable_node_list_leaves_the_run_waiting():
    """Failing a run needs proof; waiting does not. If we cannot read the nodes
    we cannot prove impossibility, so the run keeps its place in the queue."""
    driver, api, handle = _driver()
    api.pods = [_unschedulable_pod(4)]

    def _pods_only(resource, label_selector=""):
        if resource == "pods":
            return list(api.pods)
        raise RuntimeError("nodes forbidden")

    api.list = _pods_only
    assert driver.failure_reason(handle)[0] == "unschedulable"
