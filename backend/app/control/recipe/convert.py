"""apply and diff: a flat config onto a recipe, and back.

    apply(base, config) -> recipe      write each address into a copy of base
    diff(base, recipe)  -> config      the addresses at which recipe differs

They are inverses, which is the whole promise of the address scheme: a search
policy may think in flat configs, a person or an agent may edit the YAML, and
neither loses anything on the way through the other.

    apply(base, diff(base, r)) == r          (same hash; untouched text kept)
    diff(base, apply(base, c)) == canonical(base, c)

`canonical` is the config as it actually lands: aliases folded, values typed
as the list reads them back, and no-op entries (a value equal to the base's,
False on a switch that is absent and cannot be negated) dropped.

`None` removes: a flag from its list, a variable from env, a key from values
(the operator's layer merge reads `null` the same way).
"""

from __future__ import annotations

import copy
from typing import Any

from app.control.engines.env_keys import ENV_PREFIX, env_value
from app.control.engines.flags import normalize_args
from app.control.recipe import args as argv
from app.control.recipe.address import (
    ROLE_PREFIXES,
    Address,
    is_managed,
    parse,
    value_key,
)
from app.control.recipe.model import ROLES, Recipe

_MISSING = object()


def _get(tree: dict[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = tree
    for segment in path:
        if not isinstance(node, dict) or segment not in node:
            return _MISSING
        node = node[segment]
    return node


def _set(tree: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node = tree
    for segment in path[:-1]:
        child = node.get(segment)
        if not isinstance(child, dict):
            child = {}
            node[segment] = child
        node = child
    if value is None:
        node.pop(path[-1], None)
    else:
        node[path[-1]] = copy.deepcopy(value)


def _set_env(entries: list[Any], name: str, value: Any) -> list[Any]:
    out = [copy.deepcopy(e) for e in entries or []]
    found = [i for i, e in enumerate(out) if isinstance(e, dict) and e.get("name") == name]
    if value is None:
        return [e for i, e in enumerate(out) if i not in found]
    # A dict is a whole entry body (valueFrom: a secret or field ref), which a
    # plain value cannot say.
    if isinstance(value, dict):
        entry = {"name": name, **copy.deepcopy(value)}
    else:
        entry = {"name": name, "value": env_value(value)}
    if found:
        # Last one wins in a pod spec too; keep its position, drop the rest.
        keep = found[-1]
        out[keep] = entry
        return [e for i, e in enumerate(out) if i == keep or i not in found]
    out.append(entry)
    return out


def apply(base: Recipe, config: dict[str, Any]) -> Recipe:
    """`config` written into a copy of `base`. Addresses apply in order, so a
    later `values.extraArgs`-style replacement cannot happen (check refuses
    managed paths) and flags on the same list compose."""
    values = copy.deepcopy(base.values)
    for key, value in config.items():
        address = parse(key)
        if address.kind == "flag":
            values["extraArgs"] = argv.set_flag(
                base.engine, values.get("extraArgs"), _canonical_flag(base, address.name), value
            )
        elif address.kind == "role_flag":
            pd = values.setdefault("pd", {})
            role = pd.setdefault(address.role, {})
            role["extraArgs"] = argv.set_flag(
                base.engine, role.get("extraArgs"), _canonical_flag(base, address.name), value
            )
        elif address.kind == "env":
            values["env"] = _set_env(values.get("env") or [], address.name, value)
            if not values["env"]:
                values.pop("env")
        else:
            _set(values, address.path, value)
    return base.model_copy(update={"values": values})


def _canonical_flag(base: Recipe, key: str) -> str:
    args, _ = normalize_args(base.engine, {key: True})
    return next(iter(args))


def _flag_diff(prefix: str, old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in new.items():
        if key not in old or old[key] != value:
            out[prefix + key] = value
    for key in old:
        if key not in new:
            out[prefix + key] = None
    return out


def _value_diff(old: Any, new: Any, path: tuple[str, ...], out: dict[str, Any]) -> None:
    if is_managed(path):
        return
    if isinstance(old, dict) and isinstance(new, dict):
        for key in new:
            _value_diff(old.get(key, _MISSING), new[key], (*path, key), out)
        for key in old:
            if key not in new and not is_managed((*path, key)):
                out[value_key((*path, key))] = None
        return
    if old is _MISSING or old != new:
        if isinstance(new, dict) and old is not _MISSING and not isinstance(old, dict):
            out[value_key(path)] = copy.deepcopy(new)
        elif isinstance(new, dict) and old is _MISSING:
            # Spell a new section leaf by leaf, so its keys read as knobs.
            if not new:
                out[value_key(path)] = {}
            for key, child in new.items():
                _value_diff(_MISSING, child, (*path, key), out)
        else:
            out[value_key(path)] = copy.deepcopy(new)


def diff(base: Recipe, recipe: Recipe) -> dict[str, Any]:
    """The config that turns `base` into `recipe`."""
    out: dict[str, Any] = {}
    out.update(_flag_diff("", base.flags(), recipe.flags()))
    for role in ROLES:
        out.update(
            _flag_diff(
                f"{role}.",
                argv.flags(base.engine, base.role(role).get("extraArgs")),
                argv.flags(recipe.engine, recipe.role(role).get("extraArgs")),
            )
        )
    old_env, new_env = _env_entries(base), _env_entries(recipe)
    for name, value in new_env.items():
        if old_env.get(name) != value or name not in old_env:
            out[ENV_PREFIX + name] = value
    for name in old_env:
        if name not in new_env:
            out[ENV_PREFIX + name] = None
    _value_diff(base.values, recipe.values, (), out)
    return out


def _env_entries(recipe: Recipe) -> dict[str, Any]:
    """name → its value, or the entry body when it is not a plain value."""
    out: dict[str, Any] = {}
    for entry in recipe.values.get("env") or []:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        body = {k: v for k, v in entry.items() if k != "name"}
        out[str(entry["name"])] = str(body["value"]) if set(body) == {"value"} else body
    return out


def canonical(base: Recipe, config: dict[str, Any]) -> dict[str, Any]:
    """`config` as it lands on `base`: what diff would read back."""
    return diff(base, apply(base, config))


def full_config(recipe: Recipe) -> dict[str, Any]:
    """Every flag and variable the recipe sets, as one flat config, the shape
    coverage and the ledger read. Chart values other than flags and env are
    left out: against no base, every one of them would be a "knob"."""
    out: dict[str, Any] = dict(recipe.flags())
    for role in ROLES:
        for key, value in argv.flags(recipe.engine, recipe.role(role).get("extraArgs")).items():
            out[f"{role}.{key}"] = value
    for name, value in recipe.env().items():
        out[ENV_PREFIX + name] = value
    return out


def describe(address: Address) -> str:
    if address.kind == "role_flag":
        return f"{address.role} flag {address.name}"
    if address.kind == "env":
        return f"env {address.name}"
    if address.kind == "value":
        return "chart value " + value_key(address.path)
    return f"flag {address.name}"


__all__ = ["apply", "canonical", "describe", "diff", "full_config", "ROLE_PREFIXES"]
