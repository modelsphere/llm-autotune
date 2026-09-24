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
