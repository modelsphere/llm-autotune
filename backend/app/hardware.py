"""Canonical GPU types, and how to recognize one from the cluster.

A card type is not decoration: the platform's whole comparison model leans on
it. Throughput "per card" (`output_tpm_card_norm`) is normalized by card
*count*, never by card *type* — so an H100 result and an A100 result are not on
one scale, and ranking them together, or against a baseline captured on the
other chip, silently compares different silicon. That only stays correct if the
card type is a controlled value the whole platform agrees on, not a free string
one person writes "A100" and another "A100-SXM4-80GB".

So there is one vocabulary here, one function that maps a cluster GPU product
label (`nvidia.com/gpu.product` = "NVIDIA-H100-80GB-HBM3") or a legacy hand-typed
value onto it, and one list the forms offer. Teaching the platform a new card the
cluster grows into (B300 today, whatever is next) is a single line in GPU_TYPES.
"""

import re

# Canonical short names, roughly oldest -> newest. The forms offer these; a k8s
# machine's type is probed from the cluster and normalized to one of them. This
# tuple is the ONE place a new card is taught to the platform — extend it and
# the dropdowns, validation and normalizer all follow.
GPU_TYPES: tuple[str, ...] = (
    "A100",
    "A800",
    "H100",
    "H800",
    "H200",
    "H20",
    "B200",
    "B300",
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
    for gpu in GPU_TYPES:
        if re.search(rf"(?<![A-Z0-9]){gpu}(?![A-Z0-9])", s):
            return gpu
    return ""


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
