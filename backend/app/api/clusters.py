"""Clusters — the apiservers the platform may launch onto.

Before this, the k8s driver was one cluster per worker: the kubeconfig,
namespace and workload kind were process-wide `AUTOTUNE_K8S_*` settings, so a
second cluster could only be served by pointing the whole worker at it. A
machine now binds to a cluster row, and `AUTOTUNE_K8S_*` is just the DEFAULT
cluster's values (a machine whose `cluster_id` is null).

The kubeconfig is stored encrypted at rest (`app.core.secrets`, keyed off
JWT_SECRET) and is write-only through this API: it goes in as plaintext and
never comes back out.

The probe is the "before you promise a date" step — it answers the questions
that silently produced Pending pods on the first cluster: is the namespace
reachable, can this credential read nodes (capacity probe + endpoint), is the
operator's CRD installed when workload_kind=custom.
"""

from datetime import UTC

import anyio
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.control.launch.clusters import K8sClusterSettings, invalidate_cluster
from app.control.launch.k8s_api import forget_transport, get_k8s_api
from app.core.auth import get_current_user, require_admin
from app.core.config import get_settings
from app.core.secrets import encrypt_secret
from app.db.base import get_async_session
from app.db.models import Cluster, Event, Machine, User
from app.hardware import normalize_gpu_type
from app.schemas.clusters import ClusterIn, ClusterOut

router = APIRouter(prefix="/clusters", tags=["clusters"])

_GPU_PRODUCT_LABEL = "nvidia.com/gpu.product"


def _out(row: Cluster, machine_count: int = 0) -> ClusterOut:
    out = ClusterOut.model_validate(row)
    return out.model_copy(
        update={"has_kubeconfig": bool(row.kubeconfig_enc), "machine_count": machine_count}
    )


async def _counts(session: AsyncSession) -> dict[int, int]:
    rows = (
        await session.execute(
            select(Machine.cluster_id, func.count())
            .where(Machine.cluster_id.is_not(None))
            .group_by(Machine.cluster_id)
        )
    ).all()
    return {cid: n for cid, n in rows}


async def _get(cluster_id: int, session: AsyncSession) -> Cluster:
    row = await session.get(Cluster, cluster_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such cluster")
    return row


@router.get("", response_model=list[ClusterOut])
async def list_clusters(
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """Every configured cluster. Authenticated (not admin-only) because the
    Add-machine form needs it; the kubeconfig is never part of the response."""
    rows = (await session.execute(select(Cluster).order_by(Cluster.id))).scalars().all()
    counts = await _counts(session)
    return [_out(row, counts.get(row.id, 0)) for row in rows]


@router.post("", response_model=ClusterOut, status_code=status.HTTP_201_CREATED)
async def create_cluster(
    body: ClusterIn,
    user: User = Depends(require_admin),
    session: AsyncSession = Depends(get_async_session),
):
    clash = (
        await session.execute(select(Cluster).where(Cluster.name == body.name))
    ).scalar_one_or_none()
    if clash is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "cluster name taken")
    data = body.model_dump(exclude={"kubeconfig"})
    row = Cluster(**data, kubeconfig_enc=encrypt_secret(body.kubeconfig))
    session.add(row)
    session.add(
        Event(actor=user.username, kind="cluster_added", payload={"name": row.name})
    )
    await session.commit()
    await session.refresh(row)
    return _out(row)


@router.put("/{cluster_id}", response_model=ClusterOut)
async def update_cluster(
    cluster_id: int,
    body: ClusterIn,
    user: User = Depends(require_admin),
    session: AsyncSession = Depends(get_async_session),
):
    row = await _get(cluster_id, session)
    clash = (
        await session.execute(
            select(Cluster).where(Cluster.name == body.name, Cluster.id != cluster_id)
        )
    ).scalar_one_or_none()
    if clash is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "cluster name taken")
    data = body.model_dump(exclude={"kubeconfig"})
    for field, value in data.items():
        setattr(row, field, value)
    # Empty = keep the stored credential (the form cannot re-send what it never
    # received). Anything non-empty rotates it.
    if body.kubeconfig.strip():
        row.kubeconfig_enc = encrypt_secret(body.kubeconfig)
    session.add(
        Event(actor=user.username, kind="cluster_updated", payload={"name": row.name})
    )
    await session.commit()
    await session.refresh(row)
    invalidate_cluster(cluster_id)
    forget_transport(cluster_id)
    return _out(row, (await _counts(session)).get(row.id, 0))


@router.delete("/{cluster_id}")
async def delete_cluster(
    cluster_id: int,
    user: User = Depends(require_admin),
    session: AsyncSession = Depends(get_async_session),
):
    row = await _get(cluster_id, session)
    in_use = (await _counts(session)).get(row.id, 0)
    if in_use:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"cannot remove {row.name}: {in_use} machine(s) still name it. "
            "Re-point or remove them first.",
        )
    name = row.name
    await session.delete(row)
    session.add(Event(actor=user.username, kind="cluster_removed", payload={"name": name}))
    await session.commit()
    invalidate_cluster(cluster_id)
    forget_transport(cluster_id)
    return {"removed": name}


def _probe(cluster: K8sClusterSettings) -> dict:
    """Read-only capability check. Blocking k8s calls — run in a worker thread.

    Every failure is a warning, not an exception: the whole point is to report
    what a cluster *cannot* do before a run discovers it."""
    result: dict = {
        "api_mode": cluster.k8s_api_mode,
        "namespace": cluster.k8s_namespace,
        "workload_kind": cluster.k8s_workload_kind,
        "reachable": False,
        "namespace_readable": False,
        "nodes_readable": False,
        "node_count": 0,
        "gpu_types": [],
        "crd_present": None,
        "warnings": [],
    }
    if not cluster.k8s_api_mode or cluster.k8s_api_mode == "unavailable":
        result["warnings"].append("api_mode is 'unavailable' — set it to client")
        return result
    try:
        api = get_k8s_api(cluster)
    except Exception as exc:  # noqa: BLE001 — report, never raise
        result["warnings"].append(f"transport unavailable: {exc}")
        return result

    try:
        pods = api.list("pods")
        result["reachable"] = True
        result["namespace_readable"] = True
        result["pods_visible"] = len(pods)
    except Exception as exc:  # noqa: BLE001
        result["warnings"].append(
            f"cannot list pods in {cluster.k8s_namespace}: {str(exc)[:300]}"
        )

    try:
        nodes = api.list("nodes")
        result["reachable"] = True
        result["nodes_readable"] = True
        result["node_count"] = len(nodes)
        types = set()
        for node in nodes:
            labels = (node.get("metadata") or {}).get("labels") or {}
            canon = normalize_gpu_type(str(labels.get(_GPU_PRODUCT_LABEL) or ""))
            if canon:
                types.add(canon)
        result["gpu_types"] = sorted(types)
    except Exception as exc:  # noqa: BLE001
        result["warnings"].append(
            "cannot read nodes (cluster-scoped): capacity probe, endpoint "
            f"derivation and card-type provenance fall back — {str(exc)[:200]}"
        )
        if not cluster.k8s_node_host:
            result["warnings"].append(
                "no node_host set and nodes are unreadable: runs cannot build an "
                "endpoint URL — set node_host to a node/control-plane IP"
            )

    if cluster.k8s_workload_kind == "custom":
        cr = f"{cluster.k8s_cr_plural}.{cluster.k8s_cr_group}"
        try:
            api.list(cr)
            result["crd_present"] = True
        except Exception as exc:  # noqa: BLE001
            result["crd_present"] = False
            result["warnings"].append(
                f"workload_kind=custom but {cr} is not installed — use "
                f"'deployment' or install the operator ({str(exc)[:160]})"
            )
    return result


@router.post("/{cluster_id}/probe")
async def probe_cluster(
    cluster_id: int,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_async_session),
):
    """Test a cluster the way a launch will use it, and store the result so the
    Resources page can show it without re-probing on every load."""
    from datetime import datetime

    row = await _get(cluster_id, session)
    cluster = K8sClusterSettings.from_row(row, get_settings())
    result = await anyio.to_thread.run_sync(_probe, cluster)
    row.last_probe = result
    row.last_probe_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(row)
    return {"cluster": (await _cluster_out(session, row)).model_dump(mode="json"),
            "probe": result}


async def _cluster_out(session: AsyncSession, row: Cluster) -> ClusterOut:
    return _out(row, (await _counts(session)).get(row.id, 0))
