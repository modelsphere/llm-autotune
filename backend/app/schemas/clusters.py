"""Request/response shapes for the `clusters` table.

The kubeconfig goes IN as plaintext (`kubeconfig`), is encrypted at rest, and
never comes back OUT: `ClusterOut` carries `has_kubeconfig` instead, so a
credential cannot leak through a list endpoint or the UI.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ApiMode = Literal["client", "kubectl", "unavailable"]
WorkloadKind = Literal["deployment", "custom"]


class ClusterIn(BaseModel):
    """Create or edit a cluster. On edit, an empty `kubeconfig` keeps the stored
    one rather than erasing it — the form never has the plaintext to re-send."""

    name: str = Field(..., min_length=1, max_length=64)
    notes: str = ""
    api_mode: ApiMode = "client"
    # The scoped kubeconfig YAML. Stored encrypted; "" on edit = keep existing.
    kubeconfig: str = ""
    context: str = ""
    namespace: str = "autotune"
    workload_kind: WorkloadKind = "deployment"
    node_host: str = ""
    gpu_resource: str = "nvidia.com/gpu"
    runtime_class: str = "nvidia"
    tolerate_gpu_taint: bool = True
    tolerations: str = ""
    image_pull_secrets: str = ""
    model_pvc: str = ""
    model_pvc_root: str = ""
    shm_size_mb: int = Field(2048, ge=0, le=1_048_576)
    node_selector: str = ""
    service_nodeport: int = Field(0, ge=0, le=65535)
    run_ttl_seconds: int = Field(0, ge=0)
    in_cluster: bool = False
    cr_group: str = "tuning.llm-autotune.io"
    cr_version: str = "v1alpha1"
    cr_kind: str = "TuningRun"
    cr_plural: str = "tuningruns"
    cr_pod_label: str = "tuning.llm-autotune.io/run"
    engine_cpu_request: str = ""
    engine_memory_request: str = ""
    engine_cpu_limit: str = ""
    engine_memory_limit: str = ""

    @field_validator("name", "namespace")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be empty")
        return v.strip()


class ClusterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    notes: str
    api_mode: str
    has_kubeconfig: bool = False
    context: str
    namespace: str
    workload_kind: str
    node_host: str
    gpu_resource: str
    runtime_class: str
    tolerate_gpu_taint: bool
    tolerations: str
    image_pull_secrets: str
    model_pvc: str
    model_pvc_root: str
    shm_size_mb: int
    node_selector: str
    service_nodeport: int
    run_ttl_seconds: int
    in_cluster: bool
    cr_group: str
    cr_version: str
    cr_kind: str
    cr_plural: str
    cr_pod_label: str
    engine_cpu_request: str
    engine_memory_request: str
    engine_cpu_limit: str
    engine_memory_limit: str
    last_probe: dict[str, Any] = {}
    last_probe_at: datetime | None = None
    created_at: datetime | None = None
    # How many machines name this cluster — the UI's "in use" signal, and what
    # makes a delete worth refusing.
    machine_count: int = 0
