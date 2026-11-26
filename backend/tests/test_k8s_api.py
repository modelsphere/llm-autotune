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


def test_an_api_error_says_what_the_server_refused():
    """A rejected LLMService used to read only "Unprocessable Entity"."""
    from types import SimpleNamespace

    from app.control.launch.k8s_api import _why

    body = ('{"kind":"Status","message":"LLMService.serving.modelsphere.dev \\"r\\" is invalid: '
            "spec.chart.repo: Invalid value: \\\"http://c\\\": should match '^(https://|oci://).+'\"}")
    refused = SimpleNamespace(reason="Unprocessable Entity", body=body)
    assert _why(refused).startswith("Unprocessable Entity: LLMService")
    assert "spec.chart.repo" in _why(refused)
    assert _why(SimpleNamespace(reason="Forbidden", body="not json")) == "Forbidden"
    assert _why(SimpleNamespace(reason="Not Found", body=None)) == "Not Found"


def test_a_service_that_names_its_cluster_ip_is_replaced_not_created():
    """The fixed-port Service is pointed at the front door with its own
    cluster IP kept. Created, the server refused that IP as taken (422) before
    reporting the name as existing (409), so the update never happened."""
    from types import SimpleNamespace as NS

    class Refused(Exception):
        def __init__(self, status):
            self.status, self.reason, self.body = status, "Unprocessable Entity", None

    class FakeCore:
        def __init__(self):
            self.calls = []

        def create_namespaced_service(self, ns, item):
            self.calls.append("create")
            raise Refused(422)

        def replace_namespaced_service(self, name, ns, item):
            self.calls.append(("replace", item["metadata"]["resourceVersion"]))

        def read_namespaced_service(self, name, ns):
            return NS(metadata=NS(resource_version="41"))

    api = ClientApi(_s(k8s_namespace="runs"))
    core = FakeCore()
    api._core = core
    api._ApiException = Refused
    api._ensure = lambda: None
    api._sanitize = lambda obj: {"metadata": {"resourceVersion": obj.metadata.resource_version}}
    api.apply({"kind": "Service", "metadata": {"name": "r-np"},
               "spec": {"clusterIP": "10.96.0.9", "selector": {"app": "front"}}})
    assert core.calls == [("replace", "41")]


def test_client_exec_never_swaps_the_shared_clients_request(monkeypatch):
    """stream() swaps an ApiClient's request method for a websocket one while
    it runs. On the shared client two execs at once left the websocket one in
    place for good, and every later call of the process (node reads, pod
    lists) failed with "'NoneType' object has no attribute 'decode'". Each
    exec now has a client of its own."""
    import functools
    import threading

    import kubernetes.stream
    from kubernetes import client
    from kubernetes.stream.stream import _websocket_request

    cfg = client.Configuration()
    cfg.host = "https://k8s.invalid"
    api = ClientApi(_s(k8s_namespace="runs"))
    api._ensure = lambda: None
    api._cfg = cfg
    api._api_client = client.ApiClient(configuration=cfg)
    api._core = client.CoreV1Api(api._api_client)
    api._ApiException = client.rest.ApiException
    shared = api._api_client.request
    both = threading.Barrier(2, timeout=5)
    seen = []

    class Resp:
        returncode = 0

        def __init__(self):
            self.open = True

        def is_open(self):
            return self.open

        def update(self, timeout):
            self.open = False

        def peek_stdout(self):
            return False

        def peek_stderr(self):
            return False

        def close(self):
            pass

    def fake_ws(configuration, _method, url, **kwargs):
        # Both execs are inside stream() here at the same time.
        seen.append(api._api_client.request == shared)
        both.wait()
        return Resp()

    fake_stream = functools.partial(_websocket_request, fake_ws, None)
    monkeypatch.setattr(kubernetes.stream, "stream", fake_stream)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(api.exec("p", "c", ["true"], 5)))
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [(0, ""), (0, "")]
    assert seen == [True, True]
    assert api._api_client.request == shared


def test_client_load_runs_once_and_sets_core_last(monkeypatch):
    """Threads share a ClientApi: the first use loads it once, and a thread
    that finds `_core` set finds every other client set too."""
    import threading

    api = ClientApi(_s(k8s_namespace="runs"))
    loads = []
    gate = threading.Event()

    def slow_load():
        loads.append(1)
        gate.wait(2)
        api._apps = api._custom = object()
        api._core = object()

    monkeypatch.setattr(api, "_load", slow_load)
    threads = [threading.Thread(target=api._ensure) for _ in range(4)]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join()
    assert loads == [1]
    assert api._apps is not None
