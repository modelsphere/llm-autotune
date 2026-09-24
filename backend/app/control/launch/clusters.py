"""Per-cluster k8s settings: what makes a machine's pods land in *one* cluster.

The k8s driver used to read every `AUTOTUNE_K8S_*` value off the process-global
`Settings`, which is why one worker could only ever serve one cluster. This
module turns that slice of `Settings` into a value a machine can carry:

    settings (the DEFAULT cluster, i.e. today's env)
        │  from_settings()
        ▼
    K8sClusterSettings  ──►  get_k8s_api() / K8sDriver
        ▲  from_row()
        │
    clusters table row (an OVERRIDE set — anything it leaves default still
                        falls back to Settings)

`K8sClusterSettings` deliberately keeps the `k8s_*` attribute names of
`Settings`, so the driver and its renderers keep reading `settings.k8s_namespace`
whether the object is the env default or a DB cluster.

Resolution is cached briefly and never touches the database for the default
cluster (`cluster_id is None`), so a single-cluster deployment — and every test
— behaves exactly as before and needs no DB.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, fields
from typing import Any

logger = logging.getLogger(__name__)

# Every per-cluster knob, named as it is on `Settings`. This is the contract
# between the row and the driver: a field here is one a cluster may override.
_OVERRIDABLE: tuple[str, ...] = (
    "k8s_api_mode",
    "k8s_kubectl_bin",
    "k8s_namespace",
    "k8s_context",
    "k8s_kubeconfig",
    "k8s_in_cluster",
    "k8s_workload_kind",
    "k8s_cr_group",
    "k8s_cr_version",
    "k8s_cr_kind",
    "k8s_cr_plural",
    "k8s_cr_pod_label",
    "k8s_run_ttl_seconds",
    "k8s_node_host",
    "k8s_service_nodeport",
    "k8s_gpu_resource",
    "k8s_runtime_class",
    "k8s_engine_cpu_request",
    "k8s_engine_memory_request",
    "k8s_engine_cpu_limit",
    "k8s_engine_memory_limit",
    "k8s_node_selector",
    "k8s_tolerate_gpu_taint",
    "k8s_tolerations",
    "k8s_image_pull_secrets",
    "k8s_model_pvc",
    "k8s_model_pvc_root",
    "k8s_shm_size_mb",
    "k8s_call_timeout",
    "k8s_policy_cpu_request",
    "k8s_policy_memory_request",
    "k8s_policy_ttl_after_finished",
    "k8s_policy_deadline_seconds",
    "k8s_policy_priority_class",
)

# Cluster-row column -> the `k8s_*` name it overrides. Fields absent here (the
# policy defaults, call timeout, kubectl bin) are process-global for now.
_ROW_MAP: dict[str, str] = {
    "api_mode": "k8s_api_mode",
    "context": "k8s_context",
    "namespace": "k8s_namespace",
    "workload_kind": "k8s_workload_kind",
    "node_host": "k8s_node_host",
    "gpu_resource": "k8s_gpu_resource",
    "runtime_class": "k8s_runtime_class",
    "tolerate_gpu_taint": "k8s_tolerate_gpu_taint",
    "tolerations": "k8s_tolerations",
    "image_pull_secrets": "k8s_image_pull_secrets",
    "model_pvc": "k8s_model_pvc",
    "model_pvc_root": "k8s_model_pvc_root",
    "shm_size_mb": "k8s_shm_size_mb",
    "node_selector": "k8s_node_selector",
    "service_nodeport": "k8s_service_nodeport",
    "run_ttl_seconds": "k8s_run_ttl_seconds",
    "in_cluster": "k8s_in_cluster",
    "cr_group": "k8s_cr_group",
    "cr_version": "k8s_cr_version",
    "cr_kind": "k8s_cr_kind",
    "cr_plural": "k8s_cr_plural",
    "cr_pod_label": "k8s_cr_pod_label",
    "engine_cpu_request": "k8s_engine_cpu_request",
    "engine_memory_request": "k8s_engine_memory_request",
    "engine_cpu_limit": "k8s_engine_cpu_limit",
    "engine_memory_limit": "k8s_engine_memory_limit",
}


@dataclass(frozen=True)
class K8sClusterSettings:
    """The k8s_* values for ONE cluster.

    `kubeconfig_content` is the decrypted kubeconfig, held in memory for the
    life of the resolved value and never logged (repr excluded).
    """

    cluster_id: int | None = None
    cluster_name: str = "default"
    # Bumped whenever the row changes; part of a transport's cache key so an
    # edited kubeconfig builds a new client rather than reusing the old one.
    revision: str = ""
    kubeconfig_content: str = field(default="", repr=False)

    k8s_api_mode: str = "unavailable"
    k8s_kubectl_bin: str = "kubectl"
    k8s_namespace: str = "llm-autotune"
    k8s_context: str = ""
    k8s_kubeconfig: str = ""
    k8s_in_cluster: bool = False
    k8s_workload_kind: str = "deployment"
    k8s_cr_group: str = "tuning.modelsphere.dev"
    k8s_cr_version: str = "v1alpha1"
    k8s_cr_kind: str = "TuningRun"
    k8s_cr_plural: str = "tuningruns"
    k8s_cr_pod_label: str = "tuning.modelsphere.dev/run"
    k8s_run_ttl_seconds: int = 0
    k8s_node_host: str = ""
    k8s_service_nodeport: int = 0
    k8s_gpu_resource: str = "nvidia.com/gpu"
    k8s_runtime_class: str = "nvidia"
    k8s_engine_cpu_request: str = ""
    k8s_engine_memory_request: str = ""
    k8s_engine_cpu_limit: str = ""
    k8s_engine_memory_limit: str = ""
    k8s_node_selector: str = ""
    k8s_tolerate_gpu_taint: bool = True
    k8s_tolerations: str = ""
    k8s_image_pull_secrets: str = ""
    k8s_model_pvc: str = ""
    k8s_model_pvc_root: str = ""
    k8s_shm_size_mb: int = 2048
    k8s_call_timeout: int = 60
    k8s_policy_cpu_request: str = "100m"
    k8s_policy_memory_request: str = "256Mi"
    k8s_policy_ttl_after_finished: int = 86400
    k8s_policy_deadline_seconds: int = 0
    k8s_policy_priority_class: str = ""

    @property
    def cache_key(self) -> str:
        """Identity for the transport cache. Distinct clusters never share a
        client; the default cluster is keyed on its own settings object so two
        test copies don't collide."""
        if self.cluster_id is not None:
            return f"cluster:{self.cluster_id}:{self.revision}"
        return f"default:{id(self)}"

    @property
    def is_default(self) -> bool:
        return self.cluster_id is None

    @classmethod
    def from_settings(cls, settings: Any) -> K8sClusterSettings:
        """The platform-default cluster: today's global env, no DB, no row."""
        return cls(cluster_id=None, cluster_name="default", **_copyable(settings))

    @classmethod
    def from_row(cls, row: Any, settings: Any) -> K8sClusterSettings:
        """A DB cluster row over the global defaults. A column left at its
        server default still reads from `Settings`, so only what the admin
        actually changed is pinned to the row."""
        from app.core.secrets import decrypt_secret

        values = _copyable(settings)
        for column, attr in _ROW_MAP.items():
            values[attr] = getattr(row, column)
        values["k8s_api_mode"] = "unavailable" if not row.api_mode else row.api_mode
        return cls(
            cluster_id=row.id,
            cluster_name=row.name,
            revision=_revision(row),
            kubeconfig_content=decrypt_secret(row.kubeconfig_enc or ""),
            **values,
        )


def _copyable(settings: Any) -> dict[str, Any]:
    """The overridable knobs, copied off a Settings-like object. Missing
    attributes fall back to the dataclass default so a partial stand-in (a test
    clone, a future Settings) still resolves."""
    out: dict[str, Any] = {}
    defaults = {f.name: f.default for f in fields(K8sClusterSettings)}
    for name in _OVERRIDABLE:
        if hasattr(settings, name):
            out[name] = getattr(settings, name)
        elif name in defaults:
            out[name] = defaults[name]
    return out


def _revision(row: Any) -> str:
    """A cheap change marker. There is no updated_at column, so fold the parts
    that matter into one string — enough to rebuild a client when the
    credential or namespace changes."""
    return (
        f"{row.id}:{row.workload_kind}:{row.namespace}:"
        f"{hash(str(row.kubeconfig_enc or '')) & 0xFFFFFFFF}"
    )


def as_cluster(source: Any) -> K8sClusterSettings:
    """Coerce a Settings, a K8sClusterSettings, or None into the latter.
    None (and a Settings) means the platform-default cluster."""
    from app.core.config import Settings, get_settings

    if isinstance(source, K8sClusterSettings):
        return source
    if source is None:
        return K8sClusterSettings.from_settings(get_settings())
    if isinstance(source, Settings) or hasattr(source, "k8s_api_mode"):
        return K8sClusterSettings.from_settings(source)
    raise TypeError(f"cannot read k8s cluster settings from {type(source)!r}")


# -- resolution ---------------------------------------------------------------

# Cluster rows change rarely (an admin edits one) and are read on every driver
# construction, so a short TTL keeps a probe from hitting the DB per call while
# still letting an edit land without a restart. The supervisor's long-lived
# drivers are a known exception — see the design doc: a cluster edit reaches a
# running worker on its next restart.
_TTL_SECONDS = 15.0
_cache: dict[int, tuple[float, K8sClusterSettings]] = {}


def resolve_cluster(cluster_id: int | None) -> K8sClusterSettings:
    """The settings for a machine's cluster. `None` = the platform default, and
    never touches the database."""
    from app.core.config import get_settings

    if not cluster_id:
        return K8sClusterSettings.from_settings(get_settings())
    now = time.monotonic()
    hit = _cache.get(cluster_id)
    if hit is not None and now - hit[0] < _TTL_SECONDS:
        return hit[1]
    row = _load_row(cluster_id)
    if row is None:
        logger.warning("machine names cluster %s, which no longer exists", cluster_id)
        return K8sClusterSettings.from_settings(get_settings())
    resolved = K8sClusterSettings.from_row(row, get_settings())
    _cache[cluster_id] = (now, resolved)
    return resolved


def _load_row(cluster_id: int):
    # Imported here so importing the launch layer never opens a DB connection
    # (tests construct drivers against fakes with no database).
    from app.db.base import get_sync_session
    from app.db.models import Cluster

    with get_sync_session() as session:
        return session.get(Cluster, cluster_id)


def invalidate_cluster(cluster_id: int | None = None) -> None:
    """Drop the resolved cache after a write. Called by the clusters API; the
    other process (worker) picks the change up on the TTL."""
    if cluster_id is None:
        _cache.clear()
    else:
        _cache.pop(cluster_id, None)
