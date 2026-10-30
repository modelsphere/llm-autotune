"""How many pods a recipe makes, and how many cards each one claims.

The charts render one of three shapes, and each counts cards differently:

    single-node   one pod of model.gpus
    lws           a group of lws.size pods, model.gpus each
    pd            per role, replicas x size pods of pd.<role>.gpus each, plus
                  router pods with no cards; colocate puts both roles in one pod

The platform's card accounting (pools, the FIFO queue, a machine's slots)
reads `Shape`, never the flags: what the pod asks the scheduler for is what
the chart renders from these values. The flags still have to agree with it,
which `parallelism_problems` checks: an engine instance's tp x pp (x dp unless
dp-attention) is the cards of the pods it spans.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.control.recipe.model import ROLES, Recipe
from app.control.search.validation import cards_used


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class PodGroup:
    role: str  # engine | leader | worker | prefill | decode | router | colocate
    gpus: int  # per pod
    count: int  # pods


@dataclass
class Shape:
    topology: str
    groups: list[PodGroup] = field(default_factory=list)

    @property
    def total_gpus(self) -> int:
        return sum(group.gpus * group.count for group in self.groups)

    @property
    def max_pod_gpus(self) -> int:
        return max((group.gpus for group in self.groups), default=0)

    @property
    def pods(self) -> int:
        return sum(group.count for group in self.groups)

    def payload(self) -> dict[str, Any]:
        return {
            "topology": self.topology,
            "total_gpus": self.total_gpus,
            "max_pod_gpus": self.max_pod_gpus,
            "pods": [g.__dict__ for g in self.groups],
        }


# Resources a pod requests that are not accelerators. RDMA devices ride next
# to the cards on multi-node recipes and are not cards themselves.
_NOT_CARDS = ("cpu", "memory", "ephemeral-storage", "storage", "pods")


def accelerator_request(values: dict[str, Any]) -> int:
    """Cards one pod asks for under `resources`, read when `model.gpus` is 0:
    a recipe for an accelerator the chart has no key for (an Ascend card
    under volcano, `volcano.sh/Ascend910B4-1: 8`) leaves `model.gpus` 0 so the
    chart requests no `nvidia.com/gpu`, and names its cards there instead. The
    largest one, limits before requests; 0 when none is named."""
    resources = values.get("resources") or {}
    for part in ("limits", "requests"):
        counts = [
            _int(qty)
            for name, qty in (resources.get(part) or {}).items()
            if name not in _NOT_CARDS
            and not name.startswith(("hugepages-", "rdma/"))
        ]
        if any(counts):
            return max(counts)
    return 0


def pod_gpus(recipe: Recipe) -> int:
    """Cards each engine pod claims: `model.gpus`, else what its resources name."""
    return recipe.gpus or accelerator_request(recipe.values)


def shape(recipe: Recipe) -> Shape:
    values = recipe.values
    topology = recipe.topology
    if topology == "pd":
        pd = values.get("pd") or {}
        if pd.get("colocate"):
            gpus = sum(_int(recipe.role(role).get("gpus")) for role in ROLES)
            return Shape(topology, [PodGroup("colocate", gpus, 1)])
        groups = []
        for role in ROLES:
            spec = recipe.role(role)
            pods = max(1, _int(spec.get("replicas"), 1)) * max(1, _int(spec.get("size"), 1))
            groups.append(PodGroup(role, _int(spec.get("gpus")), pods))
        router = max(1, _int((pd.get("router") or {}).get("replicas"), 1))
        groups.append(PodGroup("router", 0, router))
        return Shape(topology, groups)
    if topology == "lws":
        size = max(1, _int((values.get("lws") or {}).get("size"), 2))
        gpus = pod_gpus(recipe)
        return Shape(
            topology,
            [PodGroup("leader", gpus, 1), PodGroup("worker", gpus, size - 1)],
        )
    return Shape(topology, [PodGroup("engine", pod_gpus(recipe), 1)])


def parallelism_problems(recipe: Recipe) -> list[str]:
    """Where the flags' parallelism disagrees with the cards the pods claim.
    The engine would either leave cards idle or fail to place its ranks."""
    out: list[str] = []
    topology = recipe.topology
    if topology == "pd":
        if (recipe.values.get("pd") or {}).get("colocate"):
            return out
        for role in ROLES:
            spec = recipe.role(role)
            have = _int(spec.get("gpus")) * max(1, _int(spec.get("size"), 1))
            need = cards_used(recipe.flags(role))
            if have and need != have:
                out.append(
                    f"{role}: the flags use {need} card(s) per engine but each engine's pods"
                    f" claim {have} (pd.{role}.gpus x size)"
                )
        return out
    size = max(1, _int((recipe.values.get("lws") or {}).get("size"), 2)) if topology == "lws" else 1
    have = pod_gpus(recipe) * size
    need = cards_used(recipe.flags())
    if have and need != have:
        where = "model.gpus x lws.size" if topology == "lws" else "model.gpus"
        out.append(f"the flags use {need} card(s) but the pods claim {have} ({where})")
    return out
