"""Transport selection + the ClientApi's cluster-free logic.

The transports' actual cluster calls are exercised end-to-end elsewhere (the
driver tests run over the in-memory fake; a live run validates kubectl/client
against a real cluster). What is worth unit-testing here is that `get_k8s_api`
routes by mode, and that constructing ClientApi + resolving resources needs
neither a kubeconfig nor the cluster (config load is lazy).
"""

from app.control.launch.k8s_api import (
    ClientApi,
    KubectlApi,
    UnavailableK8sApi,
    get_k8s_api,
)
from app.core.config import get_settings


def _s(**over):
    return get_settings().model_copy(update=over)


def test_get_k8s_api_routes_by_mode():
    assert isinstance(get_k8s_api(_s(k8s_api_mode="unavailable")), UnavailableK8sApi)
    assert isinstance(get_k8s_api(_s(k8s_api_mode="kubectl")), KubectlApi)
    assert isinstance(get_k8s_api(_s(k8s_api_mode="client")), ClientApi)


def test_client_api_construction_and_resolution_need_no_cluster():
    # Config is loaded lazily, so this must not touch a kubeconfig or apiserver.
    api = ClientApi(_s(
        k8s_cr_group="tuning.modelsphere.dev",
        k8s_cr_version="v1alpha1",
        k8s_cr_plural="tuningruns",
        k8s_namespace="autotune",
    ))
    assert api._ns == "autotune"
    assert api._cr() == ("tuning.modelsphere.dev", "v1alpha1", "tuningruns")
    # apply()'s conflict path re-reads via the right resource string per kind.
    assert api._resource_for_kind("Deployment") == "deployment"
    assert api._resource_for_kind("Service") == "service"
    assert api._resource_for_kind("TuningRun") == "tuningruns.tuning.modelsphere.dev"


def test_client_logs_include_the_container_before_a_crash_restart():
    """A crash-looping engine is restarted at once, so its current log is
    empty and the crash (out of memory, a refused flag) was classified as a
    bare crash_loop. The log before the restart is what explains it."""
    from types import SimpleNamespace as NS

    class FakeCore:
        def __init__(self):
            self.calls = []

        def list_namespaced_pod(self, ns, label_selector=""):
            status = NS(container_statuses=[NS(restart_count=2)])
            return NS(items=[NS(metadata=NS(name="run-1-abc"), status=status)])

        def read_namespaced_pod_log(self, name, ns, tail_lines=200, previous=False,
                                    _preload_content=True):
            self.calls.append(previous)
            # Raw, as the real client returns it with _preload_content=False.
            text = "torch.OutOfMemoryError: CUDA out of memory\n" if previous else ""
            return NS(data=text.encode())

    api = ClientApi(_s(k8s_namespace="runs"))
    api._core = FakeCore()
    api._ApiException = Exception
    text = api.logs("autotune.modelsphere.dev/run=1")
    assert "CUDA out of memory" in text
    assert "b'" not in text, "decoded text, not the repr of the bytes"
    assert api._core.calls == [True, False]


def test_client_logs_read_every_container_of_a_sidecar_pod():
    """The engine charts run a hang-watcher beside the engine; the API refuses
    a log read without a container name on such a pod."""
    from types import SimpleNamespace as NS

    class FakeCore:
        def list_namespaced_pod(self, ns, label_selector=""):
            spec = NS(containers=[NS(name="sglang"), NS(name="hang-watcher")])
            status = NS(container_statuses=[NS(name="sglang", restart_count=0)])
            return NS(items=[NS(metadata=NS(name="run-1-abc"), spec=spec, status=status)])

        def read_namespaced_pod_log(self, name, ns, tail_lines=200, previous=False,
                                    _preload_content=True, container=None):
            assert container is not None
            return NS(data=f"{container} says hi\n".encode())

    api = ClientApi(_s(k8s_namespace="runs"))
    api._core = FakeCore()
    api._ApiException = Exception
    text = api.logs("autotune.modelsphere.dev/run=1")
    assert "[run-1-abc/sglang]\nsglang says hi" in text
    assert "[run-1-abc/hang-watcher]" in text
