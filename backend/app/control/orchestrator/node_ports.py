"""A cluster's fixed NodePort: one port, one run at a time.

A cluster whose firewall lets one port through (`service_nodeport`) puts every
run's endpoint on that port, and a NodePort belongs to the whole cluster, not
to a namespace or a machine. So whatever the cards say, a second run on that
cluster cannot be reached while the first holds the port: k8s refuses its
Service ("provided port is already allocated").

The same physical cluster can be registered more than once (two machines added
as two clusters, in different launch modes), and each registration names the
port as its own. Two registrations are one cluster here when their kubeconfigs
name the same API server, or their runs are reached on the same node host.

Read-only: rows in, an answer out. The scheduler asks it before placing a run,
the launch step before applying one.
"""

from __future__ import annotations

from dataclasses import dataclass

import yaml
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.control.launch.clusters import K8sClusterSettings
from app.core.config import get_settings
from app.db.models import TERMINAL_RUN_STATES, Cluster, Machine, Run, RunKind, RunStatus


@dataclass(frozen=True)
class NodePortClaim:
    """The fixed port a machine's runs are reached on, and the cluster it is on."""

    port: int
    server: str  # the API server its kubeconfig names, "" when unknown
    host: str  # the node host runs are reached on, "" when unset

    def collides(self, other: NodePortClaim) -> bool:
        if self.port != other.port:
            return False
        return bool(
            (self.server and self.server == other.server)
            or (self.host and self.host == other.host)
        )


def claim_of(session: Session, machine: Machine) -> NodePortClaim | None:
    """The fixed NodePort this machine's runs take, or None when k8s gives each
    run its own (or the machine is not on k8s)."""
    if (machine.driver or "") != "k8s":
        return None
    row = session.get(Cluster, machine.cluster_id) if machine.cluster_id else None
    if row is None:
        return _claim_from(K8sClusterSettings.from_settings(get_settings()))
    # Asked for every machine on every pass of the queue: a kubeconfig is
    # decrypted and parsed only when a column the answer reads has changed.
    key = (
        row.id, row.workload_kind, row.endpoint, row.node_host,
        getattr(row, "service_nodeport", 0), getattr(row, "in_cluster", False),
        row.context, row.kubeconfig_enc,
    )
    if key not in _CLAIMS:
        if len(_CLAIMS) > 256:
            _CLAIMS.clear()
        _CLAIMS[key] = _claim_from(K8sClusterSettings.from_row(row, get_settings()))
    return _CLAIMS[key]


_CLAIMS: dict[tuple, NodePortClaim | None] = {}


def _claim_from(settings: K8sClusterSettings) -> NodePortClaim | None:
    port = int(settings.k8s_service_nodeport or 0)
    if not port:
        return None
    kind = settings.k8s_workload_kind
    if kind == "llmservice" and (settings.k8s_endpoint or "dns") != "nodeport":
        return None  # reached by in-cluster DNS: the port is never asked for
    if kind not in ("deployment", "llmservice"):
        return None
    return NodePortClaim(
        port=port,
        server="in-cluster" if settings.k8s_in_cluster else _api_server(settings),
        host=(settings.k8s_node_host or "").strip().lower(),
    )


def _api_server(settings: K8sClusterSettings) -> str:
    try:
        doc = yaml.safe_load(settings.kubeconfig_content or "") or {}
    except yaml.YAMLError:
        return ""
    if not isinstance(doc, dict):
        return ""
    wanted = settings.k8s_context or doc.get("current-context") or ""
    contexts = {c.get("name"): c.get("context") or {} for c in doc.get("contexts") or []}
    cluster_name = (contexts.get(wanted) or {}).get("cluster")
    clusters = {c.get("name"): c.get("cluster") or {} for c in doc.get("clusters") or []}
    cluster = clusters.get(cluster_name) if cluster_name else None
    if cluster is None and len(clusters) == 1:
        cluster = next(iter(clusters.values()))
    return str((cluster or {}).get("server") or "").strip().rstrip("/").lower()


def port_holder(
    session: Session, machine: Machine, *, exclude_run: int | None = None
) -> Run | None:
    """The run holding the fixed NodePort this machine's runs need, on any
    machine of the same cluster, or None when the port is free.

    The port is handed out with the cards: a run holds it from the moment it
    is placed until its teardown is confirmed, launched or not. So the
    scheduler, which asks with no `exclude_run`, never places a second run
    for the port. A pending run asking for itself (`exclude_run`) waits only
    for runs placed before it, which keeps runs placed outside the queue (a
    policy's launches) in the order they were made rather than each blocking
    the other. A benchmark of an endpoint the platform did not launch never
    asks for it."""
    claim = claim_of(session, machine)
    if claim is None:
        return None
    holding = or_(
        Run.status.not_in([s.value for s in TERMINAL_RUN_STATES]),
        Run.teardown_pending.is_(True),
    )
    query = (
        select(Run)
        .join(Machine, Machine.id == Run.machine_id)
        .where(
            Machine.driver == "k8s",
            Run.kind.not_in([RunKind.BASELINE.value, RunKind.EXTERNAL.value]),
            holding,
        )
        .order_by(Run.id)
    )
    if exclude_run is not None:
        query = query.where(
            Run.id != exclude_run,
            or_(Run.status != RunStatus.PENDING.value, Run.id < exclude_run),
        )
    claims: dict[int, NodePortClaim | None] = {machine.id: claim}
    for run in session.scalars(query).all():
        if run.machine_id not in claims:
            claims[run.machine_id] = claim_of(session, session.get(Machine, run.machine_id))
        theirs = claims[run.machine_id]
        if theirs is not None and claim.collides(theirs):
            return run
    return None


def port_siblings(session: Session, machine_ids: set[int]) -> set[int]:
    """Every machine whose runs take the same fixed NodePort as one of these:
    the machines one waiter's hold on the port closes to everyone behind it."""
    if not machine_ids:
        return set()
    k8s = session.scalars(select(Machine).where(Machine.driver == "k8s")).all()
    claims = {m.id: claim_of(session, m) for m in k8s}
    wanted = [claims[mid] for mid in machine_ids if claims.get(mid) is not None]
    return {
        mid for mid, claim in claims.items()
        if claim is not None and any(claim.collides(w) for w in wanted)
    }


def _reserved_for(
    session: Session, machine: Machine, campaign_id: int | None
) -> str | None:
    """Who holds the port by an admission whose run is not placed yet: a
    campaign admitted to a machine of the same cluster (occupancy.py), as
    surely as that run would hold it once placed."""
    from app.control.orchestrator.occupancy import reservations_on

    claim = claim_of(session, machine)
    for other in session.scalars(select(Machine).where(Machine.driver == "k8s")).all():
        theirs = claim_of(session, other)
        if theirs is None or not claim.collides(theirs):
            continue
        held = reservations_on(session, other, exclude_campaign=campaign_id)
        if held:
            return held[0].label
    return None


def port_busy_reason(
    session: Session,
    machine: Machine,
    *,
    exclude_run: int | None = None,
    campaign_id: int | None = None,
) -> str | None:
    """Why a run cannot be placed on (or, with `exclude_run`, launch on) this
    machine for its cluster's fixed port, or None when the port is free.
    `campaign_id` is whose admission is asking: its own does not count."""
    claim = claim_of(session, machine)
    if claim is None:
        return None
    holder = port_holder(session, machine, exclude_run=exclude_run)
    if holder is not None:
        if holder.status == RunStatus.PENDING.value and exclude_run is not None:
            return (
                f"node port {claim.port} goes to run {holder.id} (campaign "
                f"{holder.campaign_id}) first, placed before it; the cluster lets one run "
                "be reached at a time"
            )
        return (
            f"node port {claim.port} is held by run {holder.id} (campaign "
            f"{holder.campaign_id}); the cluster lets one run be reached at a time"
        )
    if exclude_run is None:
        label = _reserved_for(session, machine, campaign_id)
        if label is not None:
            return (
                f"node port {claim.port} is held for {label}, admitted before it; the "
                "cluster lets one run be reached at a time"
            )
    return None
