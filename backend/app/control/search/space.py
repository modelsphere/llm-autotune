"""What a search space expands to — the one place that answers it.

A search space has four parts:

    {
      "base": {"mem_fraction_static": 0.9},        # fixed on every run
      "grid": {"tp_size": [1, 2, 4]},              # swept independently
      "tied": [                                    # swept together
        {"tp_size": [1, 2, 4], "mem_fraction_static": [0.9, 0.85, 0.8]}
      ],
      "range": {                                   # swept over an interval
        "chunked_prefill_size": {"min": 2048, "max": 32768, "step": 2048}
      },
      "conditions": {                              # only active sometimes
        "speculative_num_steps": {"speculative_algorithm": ["EAGLE", "EAGLE3"]}
      }
    }

`grid` is a cartesian product: three values of tp × three of mem_fraction is
nine candidates. That is the wrong shape when the parameters are not really
independent — a memory fraction that is safe at tp=4 may OOM at tp=1, so eight
of those nine points are either wasted or dead on arrival. A *tied* group zips
its columns instead: the example above is three candidates, (1, 0.9),
(2, 0.85), (4, 0.8), and it composes with the rest of the grid as a single
axis, exactly as if it were one parameter whose values happen to be tuples.

A *range* is for a parameter whose interesting values are an interval rather
than a short list. Listing eight chunk sizes by hand is how a search space ends
up testing the three someone already had a hunch about; a range says "anywhere
in here" and lets whoever searches decide. It carries a mandatory `step`,
keeps the space countable — the preview, the wall-clock estimate and grid
search itself all depend on being able to say how big a night is. A policy
that would rather have the interval than the enumeration can ask for it
directly via `range_specs()`.

A *condition* marks a parameter that only means something in company:
`speculative_num_steps` is ignored unless a speculative algorithm is on. Left
unmodelled, the sweep launches four identical deployments that differ only in a
flag the engine discards, and reports four independent measurements of the same
thing. Conditions prune the inactive key BEFORE the config is hashed, so those
four collapse into one candidate and the night tests what it meant to.

Everything that needs to know what a space expands to — the plan step, the
preview endpoint, the candidate view, the report — calls this module. The count
shown before a campaign starts and the candidates it actually creates must not
be two separate derivations that can drift apart.
"""

import json
from dataclasses import dataclass
from itertools import product
from typing import Any

# A range is enumerated as min, min+step, … up to max. Bounded so a typo like
# {"min": 0, "max": 1, "step": 0.0001} is a validation error rather than a
# ten-thousand-candidate campaign nobody asked for.
MAX_RANGE_VALUES = 512

# And a cap on the whole space, because ranges multiply. Three ranges of 512
# values each is 134 million configurations — enumerating that would hang the
# preview endpoint, which the editor calls on every keystroke. Hand-listed
# grids were self-limiting; intervals are not, so the limit has to be explicit.
#
# Well above any night that could actually run: twenty trials is a good night,
# and a space of five thousand is already asking a search to search rather
# than enumerate.
MAX_SPACE_CANDIDATES = 5000


@dataclass(frozen=True)
class RangeSpec:
    """An interval a parameter may take values in.

    `step` is required. A continuous range would be strictly more expressive
    and would break the one property the rest of the platform leans on: that a
    space can say how many candidates it contains. Discretizing at a step the
    author chooses keeps grid search usable as the baseline and still gives a
    model-based policy far more room than a hand-written list — 2048 to 32768
    by 2048 is sixteen values, not the three someone would have typed.
    """

    name: str
    minimum: float
    maximum: float
    step: float

    @property
    def is_integer(self) -> bool:
        return all(
            float(v).is_integer() for v in (self.minimum, self.maximum, self.step)
        )

    def values(self) -> list[Any]:
        """Every value in the interval, inclusive of `max` when it lands on a
        step. Floats are rounded to the step's precision: 0.7 + 3*0.05 is
        0.8500000000000001 in binary floating point, which would render as a
        different `--mem-fraction-static` than the same config expressed by
        hand and hash differently from it."""
        decimals = _decimals(self.step)
        out: list[Any] = []
        index = 0
        while True:
            value = self.minimum + index * self.step
            if value > self.maximum + self.step * 1e-9:
                break
            out.append(int(round(value)) if self.is_integer else round(value, decimals))
            index += 1
            if index > MAX_RANGE_VALUES:
                break
        return out


def _decimals(step: float) -> int:
    text = repr(float(step))
    if "e" in text or "E" in text:
        return 10
    return len(text.partition(".")[2].rstrip("0")) or 0


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def tied_groups(search_space: dict[str, Any]) -> list[dict[str, list[Any]]]:
    """Declared tied groups, skipping empty ones."""
    groups = (search_space or {}).get("tied") or []
    return [g for g in groups if isinstance(g, dict) and any(g.values())]


def _grid(search_space: dict[str, Any]) -> dict[str, list[Any]]:
    return {k: v for k, v in ((search_space or {}).get("grid") or {}).items() if v}


def grid_values(search_space: dict[str, Any]) -> dict[str, list[Any]]:
    """Declared grid axes, non-empty ones only. Public counterpart of the
    accessors tied/range/conditions already have, for searches that need to
    know which kind of axis a parameter came from rather than the flattened
    `axes()` view."""
    return _grid(search_space)


def range_specs(search_space: dict[str, Any]) -> list[RangeSpec]:
    """Declared ranges, as intervals.

    Exposed for searches that can use a distribution directly. A model-based
    optimizer asking for an integer in [2048, 32768] step 2048 is doing
    something meaningfully different from one picking among sixteen unrelated
    categories, even though the two enumerate identically.
    """
    declared = (search_space or {}).get("range") or {}
    specs: list[RangeSpec] = []
    for name, spec in sorted(declared.items()):
        if not isinstance(spec, dict):
            continue
        low, high, step = (
            _as_float(spec.get("min")),
            _as_float(spec.get("max")),
            _as_float(spec.get("step")),
        )
        if low is None or high is None or step is None or step <= 0 or high < low:
            continue  # space_errors reports it; expanding must not raise
        specs.append(RangeSpec(name=name, minimum=low, maximum=high, step=step))
    return specs


def conditions(search_space: dict[str, Any]) -> dict[str, dict[str, list[Any]]]:
    """Which parameters are only active for certain values of others.

    `{"speculative_num_steps": {"speculative_algorithm": ["EAGLE", "EAGLE3"]}}`
    reads: the draft-step count means nothing unless a speculative algorithm is
    on. All clauses for one parameter must hold.
    """
    declared = (search_space or {}).get("conditions") or {}
    out: dict[str, dict[str, list[Any]]] = {}
    for name, clause in declared.items():
        if not isinstance(clause, dict):
            continue
        parsed = {
            key: list(values) if isinstance(values, list) else [values]
            for key, values in clause.items()
        }
        if parsed:
            out[name] = parsed
    return out


def _same_value(a: Any, b: Any) -> bool:
    """Compare a config value against a declared one tolerantly.

    The editor stores everything as strings, so a condition written against
    `true` has to match the boolean True, and one written against 4096 has to
    match "4096". Being strict here silently prunes a parameter the author
    meant to keep — a failure that shows up as an engine flag quietly missing.
    """
    if a == b:
        return True
    if isinstance(a, bool) or isinstance(b, bool):
        return str(a).strip().lower() == str(b).strip().lower()
    fa, fb = _as_float(a), _as_float(b)
    if fa is not None and fb is not None:
        return fa == fb
    return str(a).strip() == str(b).strip()


def prune_inactive(config: dict[str, Any], search_space: dict[str, Any]) -> dict[str, Any]:
    """Drop parameters whose conditions are not met.

    Applied before hashing, which is the whole point: two configs that differ
    only in a parameter the engine will ignore are the same deployment, and
    must be one candidate rather than two runs of the same experiment.

    Iterated to a fixpoint so conditions can chain — if a parameter is pruned,
    anything gated on it goes too.
    """
    declared = conditions(search_space)
    if not declared:
        return config
    active = dict(config)
    for _ in range(len(declared) + 1):
        removed = False
        for name, clause in declared.items():
            if name not in active:
                continue
            holds = all(
                key in active and any(_same_value(active[key], v) for v in values)
                for key, values in clause.items()
            )
            if not holds:
                del active[name]
                removed = True
        if not removed:
            break
    return active


def axes(search_space: dict[str, Any]) -> list[list[dict[str, Any]]]:
    """The independent dimensions, each as the list of partial configs it offers.

    A grid key contributes one partial config per value; a tied group
    contributes one per row of its zipped columns; a range contributes one per
    step.
    """
    out: list[list[dict[str, Any]]] = [
        [{key: value} for value in values] for key, values in sorted(_grid(search_space).items())
    ]
    for group in tied_groups(search_space):
        keys = sorted(group)
        # Zip to the shortest column. A ragged group is a mistake the editor
        # rejects before saving; at 3am the search still has to do something,
        # and dropping the unpaired tail beats raising into an empty night.
        rows = min(len(group[k]) for k in keys)
        out.append([{k: group[k][i] for k in keys} for i in range(rows)])
    for spec in range_specs(search_space):
        out.append([{spec.name: value} for value in spec.values()])
    return [axis for axis in out if axis]


def expand(search_space: dict[str, Any]) -> list[dict[str, Any]]:
    """Every distinct configuration this space declares, base included.

    Distinct is load-bearing once conditions exist: pruning an inactive
    parameter can make two points of the cartesian product identical, and
    running both would spend two machine-hours measuring one deployment.
    """
    base: dict[str, Any] = (search_space or {}).get("base") or {}
    has_conditions = bool(conditions(search_space))
    configs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for combo in product(*axes(search_space)):
        merged = dict(base)
        for part in combo:
            merged.update(part)
        if has_conditions:
            merged = prune_inactive(merged, search_space)
            key = json.dumps(merged, sort_keys=True, separators=(",", ":"), default=str)
            if key in seen:
                continue
            seen.add(key)
        configs.append(merged)
    return configs


def swept_keys(search_space: dict[str, Any]) -> list[str]:
    """Parameters this space varies — from the declaration, not from the
    candidates it produced. Diffing candidates disagrees with the declaration
    the moment two points happen to render the same value."""
    keys = list(_grid(search_space))
    for group in tied_groups(search_space):
        keys.extend(k for k in group if k not in keys)
    keys.extend(spec.name for spec in range_specs(search_space) if spec.name not in keys)
    return sorted(keys)


def deviations(config: dict[str, Any], search_space: dict[str, Any]) -> list[dict[str, Any]]:
    """Where a config steps outside the declared space. Informational, never a
    rejection: external policies are allowed to deviate if they declare it, and
    this is the shared definition of "deviate" — the platform badges with it,
    and the policy SDK ports it so both sides agree before a launch is spent.

    Kinds, one per finding:
        unknown                the space never mentions this parameter
        off_grid               a grid/tied/stepped value not among the declared ones
        out_of_range           a range parameter outside its [min, max]
        base_override          a `base` (fixed-on-every-run) parameter changed
        conditional_violation  present although its gating condition does not hold

    Judged against the RAW submitted config on purpose: canonicalization prunes
    a conditionally-inactive key, and pruning is precisely what would hide that
    the policy sent it.
    """
    space = search_space or {}
    base: dict[str, Any] = space.get("base") or {}
    grid = _grid(space)
    tied_columns: dict[str, list[Any]] = {}
    for group in tied_groups(space):
        for key, values in group.items():
            tied_columns.setdefault(key, []).extend(values)
    ranges = {spec.name: spec for spec in range_specs(space)}
    gated = conditions(space)
    pruned = prune_inactive(config, space)

    out: list[dict[str, Any]] = []
    for param in sorted(config):
        value = config[param]
        if param in gated and param not in pruned:
            out.append({"param": param, "kind": "conditional_violation", "value": value})
            continue
        if param in grid or param in tied_columns:
            declared = grid.get(param, []) + tied_columns.get(param, [])
            if not any(_same_value(value, v) for v in declared):
                out.append({"param": param, "kind": "off_grid", "value": value})
        elif param in ranges:
            spec = ranges[param]
            numeric = _as_float(value)
            if numeric is None or not (spec.minimum <= numeric <= spec.maximum):
                out.append({"param": param, "kind": "out_of_range", "value": value})
            elif not any(_same_value(value, v) for v in spec.values()):
                out.append({"param": param, "kind": "off_grid", "value": value})
        elif param in base:
            if not _same_value(value, base[param]):
                out.append({"param": param, "kind": "base_override", "value": value})
        elif param not in gated:
            out.append({"param": param, "kind": "unknown", "value": value})
    return out


def axis_product(search_space: dict[str, Any]) -> int:
    """Candidates before conditions prune anything.

    Multiplied rather than enumerated, so it stays cheap on a space far too
    large to expand — which is exactly the space whose size someone needs to
    be told about.
    """
    total = 1
    for axis in axes(search_space):
        total *= len(axis)
    return total


def too_large(search_space: dict[str, Any]) -> bool:
    """Is this space beyond what may be enumerated?

    Ask before calling `expand()` on anything a user typed. Cheap: it
    multiplies axis lengths rather than building the product.
    """
    return axis_product(search_space) > MAX_SPACE_CANDIDATES


def candidate_count(search_space: dict[str, Any]) -> int:
    """How many candidates this space produces.

    With conditions the answer is not the product of the axes — pruning
    collapses duplicates — so it is counted from the expansion rather than
    multiplied. The number shown before a night starts has to be the number of
    runs it will actually do.

    Oversized spaces fall back to the product: they are rejected at save time,
    and a count is still more useful to show than a hung request.
    """
    product = axis_product(search_space)
    if conditions(search_space) and product <= MAX_SPACE_CANDIDATES:
        return len(expand(search_space))
    return product


def space_errors(search_space: dict[str, Any]) -> list[str]:
    """Mistakes that make a space mean something other than it looks like.

    Checked before saving, because every one of them silently changes the
    candidate list rather than failing loudly.
    """
    errors = []
    grid = _grid(search_space)
    seen: dict[str, str] = dict.fromkeys(grid, "the grid")
    for index, group in enumerate(tied_groups(search_space), start=1):
        where = f"tied group {index}"
        lengths = {k: len(v) for k, v in group.items() if v}
        if len(lengths) < 2:
            errors.append(f"{where} ties fewer than two parameters — sweep it in the grid instead")
        if len(set(lengths.values())) > 1:
            spelled = ", ".join(f"{k} has {n}" for k, n in sorted(lengths.items()))
            errors.append(f"{where}: tied parameters need the same number of values ({spelled})")
        for key in lengths:
            if key in seen:
                errors.append(f"{key} appears in both {seen[key]} and {where}")
            else:
                seen[key] = where

    errors += _range_errors(search_space, seen)
    errors += _condition_errors(search_space, seen)

    # Last, and on the cheap product: a space this large cannot be expanded to
    # be checked, so the check has to not need expanding.
    total = axis_product(search_space)
    if too_large(search_space):
        errors.append(
            f"this space expands to {total:,} configurations, over the "
            f"{MAX_SPACE_CANDIDATES:,} limit — widen a step or drop an axis "
            "(a night runs on the order of twenty)"
        )
    return errors


def _range_errors(search_space: dict[str, Any], seen: dict[str, str]) -> list[str]:
    errors: list[str] = []
    declared = (search_space or {}).get("range") or {}
    for name, spec in sorted(declared.items()):
        where = f"range {name}"
        if not isinstance(spec, dict):
            errors.append(f"{where} must be an object with min, max and step")
            continue
        low, high, step = (
            _as_float(spec.get("min")),
            _as_float(spec.get("max")),
            _as_float(spec.get("step")),
        )
        if low is None or high is None:
            errors.append(f"{where} needs numeric min and max")
            continue
        if step is None:
            # Deliberately not defaulted. A range with no step is a continuous
            # one, and the platform cannot say how many candidates that is —
            # the preview, the night estimate and grid search all need a count.
            errors.append(
                f"{where} needs a step — a range without one cannot be counted, "
                "and the campaign preview has to say how big the night is"
            )
            continue
        if step <= 0:
            errors.append(f"{where}: step must be positive")
            continue
        if high < low:
            errors.append(f"{where}: max {high:g} is below min {low:g}")
            continue
        count = len(RangeSpec(name, low, high, step).values())
        if count > MAX_RANGE_VALUES:
            errors.append(
                f"{where} expands to more than {MAX_RANGE_VALUES} values — "
                "widen the step"
            )
        if name in seen:
            errors.append(f"{name} appears in both {seen[name]} and {where}")
        else:
            seen[name] = where
    return errors


def _condition_errors(search_space: dict[str, Any], seen: dict[str, str]) -> list[str]:
    errors: list[str] = []
    base = (search_space or {}).get("base") or {}
    known = set(seen) | set(base)
    for name, clause in sorted(conditions(search_space).items()):
        where = f"condition on {name}"
        if name not in known:
            errors.append(
                f"{where}: {name} is not in this space, so the condition can "
                "never apply to anything"
            )
        for key, values in sorted(clause.items()):
            if key == name:
                errors.append(f"{where}: a parameter cannot be conditional on itself")
            elif key not in known:
                errors.append(
                    f"{where}: gated on {key}, which this space never sets — "
                    f"{name} would be dropped from every candidate"
                )
            if not values:
                errors.append(f"{where}: {key} has no values to match against")
    return errors
