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
import yaml
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.control.launch.clusters import K8sClusterSettings, invalidate_cluster
from app.control.launch.k8s import K8sDriver, _node_takes_pods
from app.control.launch.k8s_api import forget_transport, get_k8s_api
from app.control.launch.locality import ANOTHER_CLUSTER, same_cluster, unreachable_by_name
from app.core.auth import get_current_user, require_admin
from app.core.config import get_settings
from app.core.secrets import encrypt_secret
from app.db.base import get_async_session
from app.db.models import (
    TERMINAL_RUN_STATES,
    TERMINAL_SESSION_STATES,
    Campaign,
    Cluster,
    Event,
    Machine,
    MachineState,
    PolicySession,
    Run,
    User,
)
from app.hardware import normalize_gpu_type
from app.schemas.clusters import (
    ClusterIn,
    ClusterOut,
    LocalityIn,
    LocalityOut,
    RegisterNodes,
    RegisterPools,
)

router = APIRouter(prefix="/clusters", tags=["clusters"])

_GPU_PRODUCT_LABEL = "nvidia.com/gpu.product"
_HOSTNAME_LABEL = "kubernetes.io/hostname"


def _kubeconfig_namespace(kubeconfig: str, context: str = "") -> str:
    """The namespace the kubeconfig's context (or its current one) names."""
    try:
        doc = yaml.safe_load(kubeconfig) or {}
    except yaml.YAMLError:
        return ""
    if not isinstance(doc, dict):
        return ""
    wanted = context or doc.get("current-context") or ""
    for entry in doc.get("contexts") or []:
        if isinstance(entry, dict) and entry.get("name") == wanted:
            return str((entry.get("context") or {}).get("namespace") or "").strip()
    return ""


def _out(row: Cluster, machine_count: int = 0) -> ClusterOut:
    out = ClusterOut.model_validate(row)
    return out.model_copy(
        update={
            "has_kubeconfig": bool(row.kubeconfig_enc),
            "machine_count": machine_count,
            "same_cluster": same_cluster(K8sClusterSettings.from_row(row, get_settings())),
        }
    )


def _refuse_unreachable(row: Cluster) -> None:
    """Runs named by Service DNS on a cluster that is not AutoTune's own would
    each load their model and then fail their health check: refuse the save."""
    if unreachable_by_name(K8sClusterSettings.from_row(row, get_settings())):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"{row.name} cannot reach runs by Service name: {ANOTHER_CLUSTER}",
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
    check: bool = False,
    user: User = Depends(require_admin),
    session: AsyncSession = Depends(get_async_session),
):
    """Add a cluster. With `check`, probe it first the way a launch will use
    it: one the platform cannot reach is not kept, so a wrong kubeconfig never
    lingers as a cluster nobody can use."""
    clash = (
        await session.execute(select(Cluster).where(Cluster.name == body.name))
    ).scalar_one_or_none()
    if clash is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "cluster name taken")
    data = body.model_dump(exclude={"kubeconfig"})
    data["namespace"] = body.namespace or _kubeconfig_namespace(body.kubeconfig, body.context)
    if not data["namespace"]:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "the kubeconfig's context names no namespace: give the namespace engine "
            "pods should run in",
        )
    row = Cluster(**data, kubeconfig_enc=encrypt_secret(body.kubeconfig))
    _refuse_unreachable(row)
    session.add(row)
    await session.flush()
    if check:
        from datetime import datetime

        cluster = K8sClusterSettings.from_row(row, get_settings())
        result = await anyio.to_thread.run_sync(_probe, cluster)
        forget_transport(row.id)
        if not result["reachable"]:
            await session.rollback()
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                {"message": f"could not reach {body.name} with this kubeconfig; nothing "
                            "was saved", "probe": result},
            )
        row.last_probe = result
        row.last_probe_at = datetime.now(UTC)
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
    data["namespace"] = (body.namespace or _kubeconfig_namespace(body.kubeconfig, body.context)
                         or row.namespace)
    moved = [f for f in ("workload_kind", "namespace") if data[f] != getattr(row, f)]
    if moved:
        live = await _live_work(session, row.id)
        if live["runs"] or live["sessions"]:
            # The platform finds its pods by kind and namespace: changing either
            # under live work would leave those pods holding GPUs, unreachable.
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {"message": f"cannot change {' and '.join(moved)} of {row.name} while "
                            "it has live work: finish or stop it first",
                 "fields": moved, **live},
            )
    for field, value in data.items():
        setattr(row, field, value)
    # Empty = keep the stored credential (the form cannot re-send what it never
    # received). Anything non-empty rotates it.
    if body.kubeconfig.strip():
        row.kubeconfig_enc = encrypt_secret(body.kubeconfig)
    try:
        _refuse_unreachable(row)
    except HTTPException:
        await session.rollback()
        raise
    session.add(
        Event(actor=user.username, kind="cluster_updated", payload={"name": row.name})
    )
    await session.commit()
    await session.refresh(row)
    invalidate_cluster(cluster_id)
    forget_transport(cluster_id)
    return _out(row, (await _counts(session)).get(row.id, 0))


async def _live_work(session: AsyncSession, cluster_id: int) -> dict:
    """What the platform still runs on this cluster: runs not finished or not
    yet torn down, and policy sessions still going, with what a person needs to
    tell a stuck one from a busy one."""
    machines = dict(
        (await session.execute(
            select(Machine.id, Machine.name).where(Machine.cluster_id == cluster_id)
        )).all()
    )
    if not machines:
        return {"runs": [], "sessions": []}
    done = [s.value for s in TERMINAL_RUN_STATES]
    runs = (
        await session.execute(
            select(Run, Campaign.name)
            .join(Campaign, Campaign.id == Run.campaign_id)
            .where(Run.machine_id.in_(machines),
                   or_(Run.status.not_in(done), Run.teardown_pending.is_(True)))
            .order_by(Run.id)
        )
    ).all()
    sessions = (
        await session.execute(
            select(PolicySession, Campaign.name)
            .join(Campaign, Campaign.id == PolicySession.campaign_id)
            .where(PolicySession.machine_id.in_(machines),
                   PolicySession.status.not_in([s.value for s in TERMINAL_SESSION_STATES]))
            .order_by(PolicySession.id)
        )
    ).all()
    return {
        "runs": [
            {"id": r.id, "campaign_id": r.campaign_id, "campaign": name,
             "machine": machines.get(r.machine_id, ""), "status": r.status,
             "teardown_pending": bool(r.teardown_pending),
             "started_at": r.started_at.isoformat() if r.started_at else None}
            for r, name in runs
        ],
        "sessions": [
            {"id": p.id, "campaign_id": p.campaign_id, "campaign": name,
             "machine": machines.get(p.machine_id, ""), "status": p.status,
             "started_at": p.started_at.isoformat() if p.started_at else None}
            for p, name in sessions
        ],
    }


@router.get("/{cluster_id}/live")
async def cluster_live_work(
    cluster_id: int,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """The runs and policy sessions still live on this cluster."""
    await _get(cluster_id, session)
    return await _live_work(session, cluster_id)


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
        "same_cluster": same_cluster(cluster),
        "warnings": [],
    }
    if unreachable_by_name(cluster):
        result["warnings"].append(f"runs are reached by Service name, but {ANOTHER_CLUSTER}")
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
            canon = normalize_gpu_type(str(labels.get(cluster.k8s_gpu_product_label) or ""))
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

    if cluster.k8s_workload_kind == "llmservice":
        from app.control.launch.k8s_api import LLMSVC_RESOURCE

        try:
            api.list(LLMSVC_RESOURCE)
            result["crd_present"] = True
        except Exception as exc:  # noqa: BLE001
            result["crd_present"] = False
            result["warnings"].append(
                f"workload_kind=llmservice but {LLMSVC_RESOURCE} cannot be listed — install "
                f"llm-operator, or grant this credential llmservices ({str(exc)[:160]})"
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


@router.post("/locality", response_model=LocalityOut)
async def cluster_locality(body: LocalityIn, _: User = Depends(require_admin)):
    """Is this kubeconfig's cluster the one AutoTune runs in? For the form,
    before saving: it decides whether runs can be reached by Service name.
    Reads the CA in the kubeconfig only; contacts nothing."""
    from types import SimpleNamespace

    cluster = SimpleNamespace(
        k8s_in_cluster=body.in_cluster, kubeconfig_content=body.kubeconfig,
        k8s_kubeconfig="", k8s_context=body.context,
    )
    return LocalityOut(same_cluster=same_cluster(cluster))


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


# -- the cluster's GPU nodes, as machines ------------------------------------------
#
# A machine on a cluster is one GPU node, pinned by its hostname label: model
# weights live on a node's disk, so a campaign picks the nodes that hold them,
# and the preflight and smoke test answer for that node. Adding a cluster lists
# its GPU nodes; registering one makes it a machine, not yet leased.


def _gpu_nodes(cluster: K8sClusterSettings) -> list[dict]:
    """Every node with GPUs (allocatable, or a GPU product label). Blocking k8s
    calls — run in a worker thread."""
    resource = cluster.k8s_gpu_resource or "nvidia.com/gpu"
    out = []
    for node in get_k8s_api(cluster).list("nodes"):
        meta = node.get("metadata") or {}
        labels = meta.get("labels") or {}
        status_ = node.get("status") or {}
        try:
            count = int((status_.get("allocatable") or {}).get(resource, 0) or 0)
        except (TypeError, ValueError):
            count = 0
        raw_type = str(labels.get(cluster.k8s_gpu_product_label) or "")
        if count <= 0 and not raw_type:
            continue
        name = str(meta.get("name") or "")
        ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                    for c in status_.get("conditions") or [])
        out.append({
            "node": name,
            "hostname": str(labels.get(_HOSTNAME_LABEL) or name),
            "gpu_count": count,
            "gpu_type": normalize_gpu_type(raw_type),
            "gpu_product": raw_type,
            "ready": ready,
            "schedulable": not (node.get("spec") or {}).get("unschedulable"),
            # Ready, and either not cordoned or cordoned for us with the
            # cluster tolerating it: a node our pods can land on.
            "takes_pods": _node_takes_pods(node, cluster),
        })
    return sorted(out, key=lambda n: n["node"])


def _pinned_hostname(machine: Machine) -> str:
    pairs = dict(
        p.split("=", 1) for p in (machine.node_selector or "").split(",") if "=" in p
    )
    return pairs.get(_HOSTNAME_LABEL, "").strip() if len(pairs) == 1 else ""


def _forbidden(text: str) -> bool:
    lowered = text.lower()
    return "forbidden" in lowered or "403" in lowered


async def _nodes_view(row: Cluster, session: AsyncSession) -> dict:
    cluster = K8sClusterSettings.from_row(row, get_settings())
    machines = (
        await session.execute(select(Machine).where(Machine.cluster_id == row.id))
    ).scalars().all()
    try:
        nodes = await anyio.to_thread.run_sync(_gpu_nodes, cluster)
    except Exception as exc:  # noqa: BLE001 — the page says why
        text = str(exc)
        if _forbidden(text):
            # A namespace-scoped credential: no nodes to list, but the
            # namespace's GPU quota can still be one pool.
            limit = await anyio.to_thread.run_sync(K8sDriver(settings=cluster)._quota_limit)
            return {
                "cluster": row.name, "namespace": row.namespace, "nodes_readable": False,
                "nodes": [], "pools": [], "gone": [],
                "quota": None if limit is None else {"gpus": limit[0], "used": limit[1]},
                "namespace_machine": next(
                    ({"id": m.id, "name": m.name} for m in machines if not m.node_selector),
                    None,
                ),
            }
        hint = ""
        if "NameResolutionError" in text or "Failed to resolve" in text:
            hint = (" The platform resolves names through its own cluster's DNS, which does "
                    "not know this API server's name. Use a kubeconfig whose server is an "
                    "address it can reach: deploy/gpu-cluster.sh writes one (--api-server "
                    "https://<address>:6443), then Clusters ▸ More ▸ Edit to replace it.")
        elif "timed out" in text or "Connection refused" in text or "Max retries" in text:
            hint = (" The platform's pods cannot reach this API server; check the network "
                    "path from the platform's cluster to it.")
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"could not read the cluster's nodes: {text[:300]}.{hint}",
        ) from exc
    by_host = {_pinned_hostname(m): m for m in machines if _pinned_hostname(m)}
    present = {n["hostname"] for n in nodes}
    pools = _pools_of(nodes, machines, cluster.k8s_gpu_product_label)
    pool_of = {p["product"]: p["machine"] for p in pools if p["machine"]}
    for n in nodes:
        m = by_host.get(n["hostname"])
        n["machine"] = {"id": m.id, "name": m.name} if m else None
        # Already inside a registered pool: registering it alone too would
        # count its cards twice.
        n["pool"] = pool_of.get(n["gpu_product"])
    for p in pools:
        p["single_nodes"] = [
            n["machine"]["name"] for n in nodes
            if n["gpu_product"] == p["product"] and n["machine"]
        ]
    gone = [
        {"id": m.id, "name": m.name, "hostname": host}
        for host, m in by_host.items() if host not in present
    ]
    return {
        "cluster": row.name, "namespace": row.namespace, "nodes_readable": True,
        "nodes": nodes, "gone": gone, "pools": pools, "quota": None,
        "namespace_machine": None,
    }


def _pool_selector(product: str, product_label: str = _GPU_PRODUCT_LABEL) -> str:
    return f"{product_label}={product}"


def _pools_of(
    nodes: list[dict], machines: list[Machine], product_label: str = _GPU_PRODUCT_LABEL,
) -> list[dict]:
    """The cluster's GPU products, each as the pool it could be registered as:
    its nodes, their cards, and the machine already spanning them, if any."""
    by_selector = {(m.node_selector or "").replace(" ", ""): m for m in machines}
    pools: dict[str, dict] = {}
    for n in nodes:
        if not n["gpu_product"]:
            continue
        pool = pools.setdefault(n["gpu_product"], {
            "product": n["gpu_product"], "gpu_type": n["gpu_type"], "nodes": [],
            "gpu_count": 0, "gpu_total": 0, "nodes_usable": 0, "node_gpu_max": 0,
        })
        pool["nodes"].append(n["node"])
        pool["gpu_total"] += n["gpu_count"]
        # Capacity is what our pods can land on: cordoned or not-Ready nodes'
        # cards are shown, not counted.
        if n.get("takes_pods", n["ready"] and n["schedulable"]):
            pool["gpu_count"] += n["gpu_count"]
            pool["nodes_usable"] += 1
            pool["node_gpu_max"] = max(pool["node_gpu_max"], n["gpu_count"])
    for pool in pools.values():
        m = by_selector.get(_pool_selector(pool["product"], product_label))
        pool["machine"] = {"id": m.id, "name": m.name} if m else None
    return sorted(pools.values(), key=lambda p: p["product"])


@router.get("/{cluster_id}/nodes")
async def cluster_nodes(
    cluster_id: int,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_async_session),
):
    """The cluster's GPU nodes, each with the machine it is registered as (if
    any), and registered machines whose node has left the cluster."""
    return await _nodes_view(await _get(cluster_id, session), session)


@router.post("/{cluster_id}/nodes")
async def register_nodes(
    cluster_id: int,
    body: RegisterNodes,
    user: User = Depends(require_admin),
    session: AsyncSession = Depends(get_async_session),
):
    """Register GPU nodes as machines: one per node, pinned by hostname, with
    its card count and type, not yet leased. A node already registered is
    skipped."""
    row = await _get(cluster_id, session)
    view = await _nodes_view(row, session)
    nodes = {n["hostname"]: n for n in view["nodes"]}
    unknown = sorted(set(body.nodes) - set(nodes))
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"not GPU nodes of {row.name}: {', '.join(unknown)}",
        )
    pooled = sorted(f"{h} (in {nodes[h]['pool']['name']})" for h in body.nodes
                    if nodes[h]["pool"] and not nodes[h]["machine"])
    if pooled:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"already part of a registered pool, so their cards would count twice: "
            f"{', '.join(pooled)}",
        )
    taken = set((await session.execute(select(Machine.name))).scalars().all())
    registered = []
    for hostname in dict.fromkeys(body.nodes):
        node = nodes[hostname]
        if node["machine"]:
            continue
        # The node's own name, unless another machine (of another cluster, or an
        # ssh box) already has it.
        name = node["node"] if node["node"] not in taken else f"{row.name}-{node['node']}"
        if name in taken or len(name) > 64:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"no free machine name for node {node['node']}: rename the machine "
                f"called {name}",
            )
        taken.add(name)
        session.add(Machine(
            name=name,
            host="",  # placed by its node selector, never by address
            driver="k8s",
            cluster_id=row.id,
            node_selector=f"{_HOSTNAME_LABEL}={hostname}",
            gpu_count=node["gpu_count"],
            gpu_type=node["gpu_type"],
            state=MachineState.AWAY.value,
        ))
        registered.append(name)
    session.add(Event(
        actor=user.username, kind="cluster_nodes_registered",
        payload={"cluster": row.name, "machines": registered},
    ))
    await session.commit()
    return {"registered": registered, **(await _nodes_view(row, session))}


@router.post("/{cluster_id}/pools")
async def register_pools(
    cluster_id: int,
    body: RegisterPools,
    user: User = Depends(require_admin),
    session: AsyncSession = Depends(get_async_session),
):
    """Register GPU products as node pools: one machine per product, selecting
    every node with that configured product label, not yet leased. A run
    on it goes to whichever of those nodes has room. A product already
    registered as a pool is skipped."""
    row = await _get(cluster_id, session)
    view = await _nodes_view(row, session)
    taken = set((await session.execute(select(Machine.name))).scalars().all())
    if body.namespace:
        return await _register_namespace_pool(row, view, taken, user, session)
    if not body.products:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "name at least one product")
    pools = {p["product"]: p for p in view["pools"]}
    unknown = sorted(set(body.products) - set(pools))
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"no GPU nodes of {row.name} have the product: {', '.join(unknown)}",
        )
    split = sorted(f"{p} ({', '.join(pools[p]['single_nodes'])})" for p in body.products
                   if pools[p]["single_nodes"] and not pools[p]["machine"])
    if split:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "some of these nodes are already registered on their own, so a pool would "
            f"count their cards twice: {'; '.join(split)}",
        )
    registered = []
    for product in dict.fromkeys(body.products):
        pool = pools[product]
        if pool["machine"]:
            continue
        kind = (pool["gpu_type"] or product).lower()
        # Two products can share a card type (two H100 variants): the second
        # takes the product's own name.
        candidates = [f"{row.name}-{kind}"[:64],
                      f"{row.name}-{_slug(product)}"[:64]]
        name = next((c for c in candidates if c not in taken), "")
        if not name:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"no free machine name for the {product} pool: rename the machine "
                f"called {candidates[0]}",
            )
        taken.add(name)
        session.add(Machine(
            name=name,
            host="",  # placed by its node selector, never by address
            driver="k8s",
            cluster_id=row.id,
            node_selector=_pool_selector(product, row.gpu_product_label),
            gpu_count=pool["gpu_count"],
            gpu_type=pool["gpu_type"],
            node_count=len(pool["nodes"]),
            node_gpu_max=pool["node_gpu_max"],
            state=MachineState.AWAY.value,
        ))
        registered.append(name)
    session.add(Event(
        actor=user.username, kind="cluster_pools_registered",
        payload={"cluster": row.name, "machines": registered},
    ))
    await session.commit()
    return {"registered": registered, **(await _nodes_view(row, session))}


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")


async def _register_namespace_pool(
    row: Cluster, view: dict, taken: set[str], user: User, session: AsyncSession
) -> dict:
    """With no node access: one machine for the whole namespace, no node
    selector, sized by the namespace's GPU quota. The cluster places each pod."""
    if view["nodes_readable"]:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "this credential can read nodes: register pools or nodes instead",
        )
    if view["quota"] is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"{row.namespace} has no readable GPU quota to size a pool by; add the "
            "machine by hand with its card count",
        )
    others = (
        await session.execute(select(Machine.name).where(Machine.cluster_id == row.id))
    ).scalars().all()
    if others:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{row.name} already has machines ({', '.join(others)}); a namespace-wide "
            "pool would count their cards twice",
        )
    name = next((c for c in (row.name, f"{row.name}-pool") if c not in taken), "")
    if not name:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"no free machine name: rename the machine called {row.name}"
        )
    session.add(Machine(
        name=name, host="", driver="k8s", cluster_id=row.id, node_selector="",
        gpu_count=view["quota"]["gpus"],
        gpu_free=max(view["quota"]["gpus"] - view["quota"]["used"], 0),
        state=MachineState.AWAY.value,
    ))
    session.add(Event(
        actor=user.username, kind="cluster_pools_registered",
        payload={"cluster": row.name, "machines": [name], "namespace": True},
    ))
    await session.commit()
    return {"registered": [name], **(await _nodes_view(row, session))}
