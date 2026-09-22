"""Multiple clusters in one process — the part that used to be impossible.

The regression these pin is concrete: `ClientApi` used to call
`config.load_kube_config()` and then `ApiClient()`, which reads the PROCESS
default. Loading a second cluster's kubeconfig silently re-pointed the first
client at it. Every test here exists because a worker now serves several
clusters from one process.
"""

from types import SimpleNamespace

import pytest

from app.control.launch.clusters import (
    K8sClusterSettings,
    as_cluster,
    invalidate_cluster,
)
from app.control.launch.k8s import K8sDriver
from app.control.launch.k8s_api import ClientApi, get_k8s_api
from app.core.config import get_settings
from app.core.secrets import encrypt_secret


def _kubeconfig(server: str, name: str) -> str:
    return f"""
apiVersion: v1
kind: Config
clusters:
- cluster: {{server: {server}, insecure-skip-tls-verify: true}}
  name: {name}
contexts:
- context: {{cluster: {name}, user: u, namespace: ns-{name}}}
  name: ctx-{name}
current-context: ctx-{name}
users:
- name: u
  user: {{token: tok-{name}}}
"""


def _cluster(server: str, name: str, cid: int = 1) -> K8sClusterSettings:
    return K8sClusterSettings(
        cluster_id=cid,
        cluster_name=name,
        revision="r1",
        kubeconfig_content=_kubeconfig(server, name),
        k8s_api_mode="client",
        k8s_namespace=f"ns-{name}",
    )


def test_two_clusters_get_two_isolated_clients():
    """The linchpin: both clients keep their OWN apiserver after both load."""
    a = ClientApi(_cluster("https://cluster-a.example:6443", "a"))
    b = ClientApi(_cluster("https://cluster-b.example:6443", "b"))
    a._ensure()
    b._ensure()
    assert a._api_client.configuration.host == "https://cluster-a.example:6443"
    assert b._api_client.configuration.host == "https://cluster-b.example:6443"
    # And re-touching A does not drift toward B (the old global-default bug).
    a._ensure()
    assert a._api_client.configuration.host == "https://cluster-a.example:6443"


def test_cluster_namespace_comes_from_the_cluster_not_globals():
    a = ClientApi(_cluster("https://a.example:6443", "a"))
    assert a._ns == "ns-a"


def test_get_k8s_api_caches_db_clusters_and_not_the_default():
    a1 = get_k8s_api(_cluster("https://a.example:6443", "a", cid=1))
    a2 = get_k8s_api(_cluster("https://a.example:6443", "a", cid=1))
    b = get_k8s_api(_cluster("https://b.example:6443", "b", cid=2))
    assert a1 is a2, "the same cluster reuses its transport"
    assert a1 is not b

    # The default cluster is deliberately uncached: it is built from whatever
    # Settings the caller holds, so two callers must not collide.
    d1 = get_k8s_api(get_settings())
    d2 = get_k8s_api(get_settings())
    assert d1 is not d2


def test_as_cluster_copies_every_knob_from_settings():
    settings = get_settings().model_copy(
        update={"k8s_namespace": "somewhere", "k8s_shm_size_mb": 4096}
    )
    cluster = as_cluster(settings)
    assert cluster.cluster_id is None
    assert cluster.is_default
    assert cluster.k8s_namespace == "somewhere"
    assert cluster.k8s_shm_size_mb == 4096


def _row(**over) -> SimpleNamespace:
    base = dict(
        id=7, name="gpu-cluster-a", notes="", api_mode="client",
        kubeconfig_enc=encrypt_secret(_kubeconfig("https://cluster-a.example:6443", "h")),
        context="", namespace="gpu-test", workload_kind="deployment",
        node_host="198.51.100.10", gpu_resource="nvidia.com/gpu", runtime_class="",
        tolerate_gpu_taint=True, tolerations="", image_pull_secrets="",
        model_pvc="", model_pvc_root="", shm_size_mb=16384, node_selector="",
        service_nodeport=0, run_ttl_seconds=0, in_cluster=False,
        cr_group="tuning.llm-autotune.io", cr_version="v1alpha1", cr_kind="TuningRun",
        cr_plural="tuningruns", cr_pod_label="tuning.llm-autotune.io/run",
        engine_cpu_request="16", engine_memory_request="64Gi",
        engine_cpu_limit="32", engine_memory_limit="128Gi",
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_from_row_overrides_only_what_the_cluster_sets():
    cluster = K8sClusterSettings.from_row(_row(), get_settings())
    assert cluster.cluster_id == 7
    assert cluster.cluster_name == "gpu-cluster-a"
    assert cluster.k8s_namespace == "gpu-test"
    assert cluster.k8s_node_host == "198.51.100.10"
    assert cluster.k8s_shm_size_mb == 16384
    assert cluster.k8s_engine_cpu_request == "16"
    assert cluster.kubeconfig_content.startswith("\napiVersion: v1")
    # A knob the row does not own still comes from the global default.
    assert cluster.k8s_call_timeout == get_settings().k8s_call_timeout


def test_from_row_treats_empty_api_mode_as_unavailable():
    cluster = K8sClusterSettings.from_row(_row(api_mode=""), get_settings())
    assert cluster.k8s_api_mode == "unavailable"


def test_driver_cache_separates_two_clusters(monkeypatch):
    """`get_driver` must hand back a different driver per cluster, or two k8s
    clusters would share one client. The DB resolver is stubbed — this is about
    the keying, not the row lookup."""
    def fake_resolve(cluster_id):
        return K8sClusterSettings(
            cluster_id=cluster_id, cluster_name=f"c{cluster_id}", k8s_api_mode="unavailable"
        )

    monkeypatch.setattr("app.control.launch.k8s.resolve_cluster", fake_resolve)
    from app.control.launch import get_driver

    d1 = get_driver("k8s", cluster_id=1)
    d2 = get_driver("k8s", cluster_id=2)
    d1_again = get_driver("k8s", cluster_id=1)
    assert isinstance(d1, K8sDriver)
    assert d1 is not d2
    assert d1 is not d1_again, "get_driver builds a fresh driver; the supervisor caches"
    assert d1.settings.cluster_id == 1 and d2.settings.cluster_id == 2


def test_invalidate_cluster_clears_the_resolved_cache():
    # Populate then clear; the internal dict is the observable.
    from app.control.launch import clusters

    clusters._cache[99] = (0.0, K8sClusterSettings(cluster_id=99))
    invalidate_cluster(99)
    assert 99 not in clusters._cache


def test_unavailable_mode_yields_the_explaining_transport():
    from app.control.launch.k8s_api import UnavailableK8sApi

    cluster = K8sClusterSettings(k8s_api_mode="unavailable")
    assert isinstance(get_k8s_api(cluster), UnavailableK8sApi)


@pytest.mark.parametrize("mode", ["client", "kubectl"])
def test_modes_route_per_cluster(mode):
    cluster = K8sClusterSettings(cluster_id=5, revision="r", k8s_api_mode=mode)
    api = get_k8s_api(cluster)
    assert api.cluster.cluster_id == 5
