"""Static validation — funnel stage 1. No deploy, no cost.

Checks a candidate config against machine shape and basic engine constraints.
Extend by appending to CHECKS; each check returns an error string or None.

A rejection here costs nothing and saves a machine-hour, which makes a WRONG
rejection expensive in the other direction: the candidate never runs, and the
report says "invalid" as if the engine had refused it. So every check has to
model what the engine actually does, not what the flag names suggest.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class ValidationContext:
    gpu_count: int  # GPUs on the target machine class — or ACROSS a node group
    engine: str     # sglang | vllm
    # How many machines the deployment spans. 1 = single node, which is every
    # campaign that pins no group. >1 = the group's member count, and then
    # `gpu_count` is the group's TOTAL capacity so the world size must divide.
    nodes: int = 1


CheckFn = Callable[[dict[str, Any], ValidationContext], str | None]

# sglang accepts --tp/--dp/--pp as well as the long forms; vllm uses its own
# names. Any of these may appear in a search space, so all are recognized.
_TP_KEYS = ("tp", "tp_size", "tensor_parallel_size")
_DP_KEYS = ("dp", "dp_size", "data_parallel_size")
_PP_KEYS = ("pp", "pp_size", "pipeline_parallel_size")
_EP_KEYS = ("ep", "ep_size", "expert_parallel_size")
_DP_ATTENTION_KEYS = ("enable_dp_attention", "enable_dp_attn")


def _int_or_none(value: Any) -> int | None:
    """None for anything that is not a whole number — a garbage value is a
    validation error, not a reason for the search to raise."""
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_int(args: dict[str, Any], keys: tuple[str, ...], default: int = 1) -> int:
    for key in keys:
        if key in args:
            parsed = _int_or_none(args[key])
            return default if parsed is None else parsed
    return default


def _flag_on(args: dict[str, Any], keys: tuple[str, ...]) -> bool:
    """A switch is on unless it is spelled off. The editor stores booleans as
    the strings "true"/"false", and "false" is truthy in Python."""
    for key in keys:
        if key in args:
            value = args[key]
            if isinstance(value, str):
                return value.strip().lower() not in ("", "false", "0", "no", "off")
            return bool(value)
    return False


def widest_cards(search_space: dict[str, Any] | None) -> int:
    """Cards the largest configuration in a search space would occupy — the
    slice a policy session needs when it shares a machine. 0 when the space
    cannot be expanded (unknown), so callers can fall back to the whole box."""
    from app.control.search.space import expand  # local: space has no deps on us

    try:
        return max((cards_used(c) for c in expand(search_space or {})), default=0)
    except Exception:
        return 0


def cards_used(args: dict[str, Any]) -> int:
    """GPUs a config occupies.

    Normally tp × dp × pp: each data-parallel replica is its own copy of the
    model, spanning tp × pp cards.

    Data-parallel *attention* is the exception, and it is the common MoE
    deployment: it splits attention across ranks INSIDE the tensor-parallel
    group rather than adding replicas, so `--tp 4 --dp 4 --enable-dp-attention`
    is four GPUs, not sixteen — sglang requires dp to divide tp precisely
    because they share one world. Multiplying there rejects the configuration
    most worth trying on an MoE model.

    Expert parallelism (`--ep`) shards experts over cards that already exist;
    it never adds any.
    """
    tp = max(1, _first_int(args, _TP_KEYS))
    dp = max(1, _first_int(args, _DP_KEYS))
    pp = max(1, _first_int(args, _PP_KEYS))
    if _flag_on(args, _DP_ATTENTION_KEYS):
        return tp * pp
    return tp * dp * pp


def _shape(args: dict[str, Any]) -> str:
    """How the card count was arrived at, so a rejection can be argued with."""
    tp = max(1, _first_int(args, _TP_KEYS))
    dp = max(1, _first_int(args, _DP_KEYS))
    pp = max(1, _first_int(args, _PP_KEYS))
    parts = [f"tp={tp}"]
    if _flag_on(args, _DP_ATTENTION_KEYS):
        parts.append(f"dp={dp} folded in by dp attention")
    elif dp > 1:
        parts.append(f"dp={dp}")
    if pp > 1:
        parts.append(f"pp={pp}")
    return ", ".join(parts)


def _check_parallelism_fits(args: dict[str, Any], ctx: ValidationContext) -> str | None:
    if ctx.gpu_count <= 0:
        # No registered machine reports any cards — a CPU-only test host, or a
        # fleet not described yet. A fit cannot be checked against an unknown
        # capacity, and rejecting everything would make the mock flow untestable.
        return None
    needed = cards_used(args)
    if needed > ctx.gpu_count:
        return f"needs {needed} GPUs ({_shape(args)}), machine has {ctx.gpu_count}"
    return None


def _check_dp_attention_divides_tp(
    args: dict[str, Any], ctx: ValidationContext
) -> str | None:
    """Data-parallel attention partitions the tp group, so the group has to
    partition evenly — sglang refuses to start otherwise."""
    if not _flag_on(args, _DP_ATTENTION_KEYS):
        return None
    tp = max(1, _first_int(args, _TP_KEYS))
    dp = max(1, _first_int(args, _DP_KEYS))
    if tp % dp:
        return f"dp attention needs dp={dp} to divide tp={tp}"
    return None


def _check_expert_parallel_fits(args: dict[str, Any], ctx: ValidationContext) -> str | None:
    """Experts are sharded over the cards the config already has; asking for
    more shards than there are ranks is not a bigger deployment, it is a
    startup error."""
    if not any(key in args for key in _EP_KEYS):
        return None
    ep = _first_int(args, _EP_KEYS)
    world = cards_used(args)
    if ep > world:
        return f"ep={ep} exceeds the {world} GPU(s) this config runs on ({_shape(args)})"
    return None


def _check_mem_fraction(args: dict[str, Any], ctx: ValidationContext) -> str | None:
    raw = args.get("mem_fraction_static", args.get("gpu_memory_utilization"))
    if raw is None:
        return None
    try:
        frac = float(raw)
    except (TypeError, ValueError):
        return f"memory fraction {raw!r} is not a number"
    if not (0.1 <= frac <= 0.99):
        return f"memory fraction {frac} outside sane range [0.1, 0.99]"
    return None


def _check_positive_ints(args: dict[str, Any], ctx: ValidationContext) -> str | None:
    for key in (*_TP_KEYS, *_DP_KEYS, *_PP_KEYS, *_EP_KEYS, "max_running_requests"):
        if key not in args:
            continue
        parsed = _int_or_none(args[key])
        if parsed is None:
            return f"{key}={args[key]!r} is not a whole number"
        if parsed < 1:
            return f"{key} must be >= 1"
    return None


def _check_multi_node_shape(args: dict[str, Any], ctx: ValidationContext) -> str | None:
    """A config must divide evenly across the group's nodes.

    `gpu_count` is the group's TOTAL capacity for a node-group campaign, so
    `_check_parallelism_fits` has already said whether the config fits at all;
    this says whether it can be SPLIT. sglang gives every rank the same
    --tp-size/--pp-size and expects the world to divide across the ranks, so a
    world that does not is a launch-time error rather than a smaller job — and
    rejected here it costs no machine time, while at launch it costs a window.
    """
    if ctx.nodes <= 1:
        return None
    if ctx.engine != "sglang":
        return (
            f"{ctx.engine} multi-node deployment is not supported yet; use sglang "
            "for a node group"
        )
    world = cards_used(args)
    if world % ctx.nodes:
        return (
            f"{world} GPUs ({_shape(args)}) do not divide across {ctx.nodes} nodes; "
            "every node must get the same number of cards"
        )
    return None


CHECKS: list[CheckFn] = [
    _check_positive_ints,
    _check_dp_attention_divides_tp,
    _check_parallelism_fits,
    _check_multi_node_shape,
    _check_expert_parallel_fits,
    _check_mem_fraction,
]


def validate_config(args: dict[str, Any], ctx: ValidationContext) -> str | None:
    """Returns an error string for the first failed check, or None if valid."""
    for check in CHECKS:
        error = check(args, ctx)
        if error is not None:
            return error
    return None
