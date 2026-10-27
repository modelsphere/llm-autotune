"""Knob addresses: how a flat config names a place in a recipe.

Every search policy, the coverage map, the labels and the agent speak a flat
`{key: value}` config. Rather than replace that with a document format each of
them would have to learn, a key is read as an *address* into the recipe:

    tp_size                      a flag in the top-level `extraArgs`
    env.SGLANG_X                 an entry of `env`
    prefill.mem_fraction_static  a flag in `pd.prefill.extraArgs`
    decode.max_running_requests  a flag in `pd.decode.extraArgs`
    values.pd.prefill.replicas   any chart value, by its dotted path
    values.resources.limits["rdma/hca_shared"]   a segment with dots, quoted

The first two are what configs have always meant, so every stored config and
search space keeps its meaning. A flag never contains a dot, so the prefixes
cannot collide with one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.control.engines.env_keys import ENV_PREFIX

VALUES_PREFIX = "values."
ROLE_PREFIXES = ("prefill.", "decode.")

# The value paths a flag or env address already reaches. A `values.` address
# onto one of them would be a second spelling of the same fact, so `diff`
# never emits one and `check` refuses one.
MANAGED_PATHS: tuple[tuple[str, ...], ...] = (
    ("extraArgs",),
    ("env",),
    ("pd", "prefill", "extraArgs"),
    ("pd", "decode", "extraArgs"),
)

_SEGMENT = re.compile(r'\["((?:[^"\\]|\\.)*)"\]|([^.\[\]]+)')


@dataclass(frozen=True)
class Address:
    kind: str  # flag | env | role_flag | value
    name: str  # the flag key, the env name, or "" for a value
    role: str = ""  # prefill | decode, for role_flag
    path: tuple[str, ...] = ()  # for value


class BadAddress(ValueError):
    pass


def parse_path(text: str) -> tuple[str, ...]:
    """`a.b["c.d"].e` → ("a", "b", "c.d", "e")."""
    out: list[str] = []
    pos = 0
    while pos < len(text):
        if text[pos] == ".":
            pos += 1
            continue
        match = _SEGMENT.match(text, pos)
        if match is None:
            raise BadAddress(f"cannot read the value path {text!r} at {text[pos:]!r}")
        quoted, bare = match.groups()
        out.append(bare if bare is not None else re.sub(r"\\(.)", r"\1", quoted))
        pos = match.end()
    if not out:
        raise BadAddress("an empty value path")
    return tuple(out)


def format_path(path: tuple[str, ...]) -> str:
    parts = []
    for segment in path:
        if re.fullmatch(r"[A-Za-z0-9_-]+", segment):
            parts.append(("." if parts else "") + segment)
        else:
            parts.append('["' + segment.replace('"', '\\"') + '"]')
    return "".join(parts)


def parse(key: str) -> Address:
    if key.startswith(ENV_PREFIX):
        name = key[len(ENV_PREFIX) :]
        if not name:
            raise BadAddress(f"{key!r} names no variable")
        return Address("env", name)
    if key.startswith(VALUES_PREFIX):
        return Address("value", "", path=parse_path(key[len(VALUES_PREFIX) :]))
    for prefix in ROLE_PREFIXES:
        if key.startswith(prefix):
            name = key[len(prefix) :]
            if not name or "." in name:
                raise BadAddress(f"{key!r}: a role flag is {prefix}<flag>")
            return Address("role_flag", name, role=prefix[:-1])
    if "." in key:
        raise BadAddress(
            f"{key!r} is not an address: a flag has no dot; chart values are values.<path>"
        )
    return Address("flag", key)


def value_key(path: tuple[str, ...]) -> str:
    return VALUES_PREFIX + format_path(path)


def is_managed(path: tuple[str, ...]) -> bool:
    return any(path[: len(m)] == m for m in MANAGED_PATHS)
