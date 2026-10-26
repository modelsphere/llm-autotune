"""Canonical accelerator types, and how to recognize one from the cluster.

A card type is not decoration: the platform's whole comparison model leans on
it. Throughput "per card" (`output_tpm_card_norm`) is normalized by card
*count*, never by card *type* — so an H100 result and an A100 result are not on
one scale, and ranking them together, or against a baseline captured on the
other chip, silently compares different silicon. That only stays correct if the
card type is a controlled value the whole platform agrees on, not a free string
one person writes "A100" and another "A100-SXM4-80GB".

So there is one vocabulary here, one function that maps a configured cluster product
label (e.g. `nvidia.com/gpu.product` = "NVIDIA-H100-80GB-HBM3") or a legacy hand-typed
value onto it, and one list the forms offer. Teaching the platform a new card the
cluster grows into (B300 today, whatever is next) is a single line in GPU_TYPES.
"""

import re

# Canonical short names, roughly oldest -> newest. The forms offer these; a k8s
# machine's type is probed from the cluster and normalized to one of them. This
# tuple drives the dropdowns and validation. Extend the normalizer too when
# the cluster uses a different spelling (e.g. Ascend's bare chip name).
GPU_TYPES: tuple[str, ...] = (
    "A100",
    "A800",
    "H100",
    "H800",
    "H200",
    "H20",
    "B200",
    "B300",
    "ASCEND-910B4-1",
)


def normalize_gpu_type(raw: str) -> str:
    """A cluster GPU product string or a legacy value -> a canonical GPU_TYPES
    entry, or "" when it matches none.

    "" is the deliberate answer for an unknown card: better to flag it (and add
    it to GPU_TYPES) than to keep a one-off string that no baseline or dropdown
    will ever match. Case- and separator-insensitive, and the model token is
    matched on a boundary so "A100" is not read out of "A1000" and "H20" is not
    read out of "H200".
    """
    if not raw:
        return ""
    s = raw.upper().replace("NVIDIA-", "").replace("NVIDIA ", "").replace("_", "-")
    # Cluster chip labels omit the vendor; accept common qualified spellings too.
    if s in {"910B4-1", "ASCEND910B4-1", "ASCEND 910B4-1", "HUAWEI-ASCEND910B4-1"}:
        return "ASCEND-910B4-1"
    for gpu in GPU_TYPES:
        if re.search(rf"(?<![A-Z0-9]){gpu}(?![A-Z0-9])", s):
            return gpu
    return ""


# What a card is, in the words engine docs use when a switch only applies to
# some hardware ("AMD only", "Hopper and newer"): vendor and architecture.
_HARDWARE_TAGS: dict[str, frozenset[str]] = {
    "A100": frozenset({"nvidia", "ampere"}),
    "A800": frozenset({"nvidia", "ampere"}),
    "H100": frozenset({"nvidia", "hopper"}),
    "H800": frozenset({"nvidia", "hopper"}),
    "H200": frozenset({"nvidia", "hopper"}),
    "H20": frozenset({"nvidia", "hopper"}),
    "B200": frozenset({"nvidia", "blackwell"}),
    "B300": frozenset({"nvidia", "blackwell"}),
    "ASCEND-910B4-1": frozenset({"npu", "ascend"}),
}


def hardware_tags(gpu_type: str) -> frozenset[str]:
    """Vendor and architecture of a canonical GPU type; empty when unknown."""
    return _HARDWARE_TAGS.get(normalize_gpu_type(gpu_type), frozenset())


def is_known_gpu_type(value: str) -> bool:
    """True for a canonical entry. "" (any card) is not a type, so it is False —
    callers that allow the wildcard check for it themselves."""
    return value in GPU_TYPES


def coerce_gpu_type(raw: str) -> str:
    """Normalize for storage, raising ValueError on an unrecognized non-empty
    value. "" passes through as the "any card" wildcard. Used by the request
    schemas so a typo is a 422 at entry, not a card type nothing compares to."""
    if not raw or not raw.strip():
        return ""
    canonical = normalize_gpu_type(raw)
    if not canonical:
        raise ValueError(
            f"unknown GPU type {raw!r}; expected one of {', '.join(GPU_TYPES)} "
            "(or empty for any card)"
        )
    return canonical


_HOSTNAME_LABEL = "kubernetes.io/hostname"


def is_node_pool(machine) -> bool:
    """A k8s machine whose selector does not pin one node by hostname: a GPU
    type label, or no selector at all. Its runs go wherever the scheduler finds
    room among the matching nodes, so a session takes a share of it rather than
    the whole of it. Takes a Machine row or a MachineInfo."""
    if (getattr(machine, "driver", "") or "") != "k8s":
        return False
    selector = getattr(machine, "node_selector", "") or ""
    return not any(
        pair.split("=", 1)[0].strip() == _HOSTNAME_LABEL for pair in selector.split(",")
    )


def record_capacity(machine, probe: dict) -> bool:
    """Write a capacity probe onto a machine row; True if it said anything.
    A probe that matched no nodes leaves the row alone: that is a selector
    mistake, not a reason to zero what was there."""
    from datetime import UTC, datetime

    if not (probe.get("supported") and probe.get("node_count")):
        return False
    machine.gpu_count = probe.get("gpu_count", machine.gpu_count)
    if probe.get("gpu_type"):
        machine.gpu_type = probe["gpu_type"]
    machine.node_count = int(probe.get("node_count") or 0)
    machine.node_gpu_max = int(probe.get("node_gpu_max") or 0)
    machine.gpu_free = probe.get("gpu_free")
    machine.capacity_checked_at = datetime.now(UTC)
    return True
