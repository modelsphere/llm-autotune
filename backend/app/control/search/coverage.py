"""What part of a declared search space a session has actually searched.

Authoritative because it is derived from the platform's own trial ledger
against the platform's own space expansion — not from anything the policy
claims. Every trial is already canonicalized into the platform's coordinate
system (`_canonical` in the policy API: base merged, conditions pruned, then
`CandidateConfig(...).hash`), and `space.expand()` enumerates that same system,
so the two sets of hashes are directly comparable.

The policy's self-reported coverage (an estimate for an infinite space, its
deliberate skips, self-served work) rides along as `policy_declared`, badged as
what it is: the supplement the platform cannot see, never the source of truth.
"""

from __future__ import annotations

from typing import Any

from app.control.search import space
from app.control.search.base import CandidateConfig
from app.schemas.policy import (
    AxisCoverage,
    AxisValueCoverage,
    PolicyCoverage,
    SpaceCoverage,
)

# A big enumerable space could otherwise dump thousands of untried configs into
# one response; the count is still exact, only the enumeration is capped.
MAX_REMAINING = 200


def _declared_values(search_space: dict[str, Any]) -> dict[str, list[Any]]:
    """Every value each swept parameter is declared to take, by parameter.

    Grid and tied columns contribute their listed values; a range contributes
    its enumerated steps. Tied groups are flattened per-parameter here on
    purpose: the cell math already honours the tie (a tied group is one axis in
    `expand()`), so the per-axis view is free to show each parameter's own
    reach independently.
    """
    out: dict[str, list[Any]] = {}
    for key, values in space.grid_values(search_space).items():
        out.setdefault(key, [])
        for v in values:
            if v not in out[key]:
                out[key].append(v)
    for group in space.tied_groups(search_space):
        for key, values in group.items():
            out.setdefault(key, [])
            for v in values:
                if v not in out[key]:
                    out[key].append(v)
    for spec in space.range_specs(search_space):
        out.setdefault(spec.name, [])
        for v in spec.values():
            if v not in out[spec.name]:
                out[spec.name].append(v)
    return out


def _project(config: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    return {k: config[k] for k in keys if k in config}


def space_coverage(
    search_space: dict[str, Any],
    trials: list[tuple[str, dict[str, Any]]],
    policy_declared: dict[str, Any] | None = None,
) -> SpaceCoverage:
    """Coverage of `search_space` by `trials` (each a ``(config_hash, config)``,
    both already canonical). `policy_declared` is the raw last coverage
    heartbeat, echoed back badged as self-reported.
    """
    search_space = search_space or {}
    swept = space.swept_keys(search_space)
    trial_hashes = {h for h, _ in trials}

    enumerable = not space.too_large(search_space)
    cells_total: int | None = None
    cells_covered = 0
    off_space = 0
    remaining: list[dict[str, Any]] = []

    if enumerable:
        expected: dict[str, dict[str, Any]] = {}
        for cfg in space.expand(search_space):
            expected[CandidateConfig(engine_args=cfg).hash] = cfg
        cells_total = len(expected)
        covered = set(expected) & trial_hashes
        cells_covered = len(covered)
        off_space = len(trial_hashes - set(expected))
        for h, cfg in expected.items():
            if h not in trial_hashes:
                if len(remaining) < MAX_REMAINING:
                    remaining.append(_project(cfg, swept))
    else:
        # Cannot enumerate, so "how many landed off-space" is not knowable; the
        # per-axis view below still tells the honest story of what was reached.
        off_space = 0

    # Per-axis: for each declared value, how many trials reached it. Works
    # whether or not the whole space is enumerable.
    declared = _declared_values(search_space)
    axes: list[AxisCoverage] = []
    for param in swept:
        values = declared.get(param, [])
        vcov: list[AxisValueCoverage] = []
        covered_here = 0
        for value in values:
            hits = sum(
                1
                for _h, cfg in trials
                if param in cfg and space._same_value(cfg[param], value)
            )
            if hits:
                covered_here += 1
            vcov.append(AxisValueCoverage(value=value, covered=bool(hits), trials=hits))
        axes.append(
            AxisCoverage(param=param, values=vcov, covered=covered_here, total=len(values))
        )

    declared_model: PolicyCoverage | None = None
    if policy_declared:
        try:
            declared_model = PolicyCoverage.model_validate(policy_declared)
        except Exception:
            declared_model = None

    return SpaceCoverage(
        enumerable=enumerable,
        cells_total=cells_total,
        cells_covered=cells_covered,
        off_space=off_space,
        remaining=remaining,
        axes=axes,
        policy_declared=declared_model,
    )
