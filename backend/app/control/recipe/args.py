"""A chart's `extraArgs` list, read and edited as flags.

The sglang and vllm charts append `extraArgs` to the engine's command line
verbatim, so the list is a command line in pieces, and every spelling an
argparse accepts can appear in it:

    --tp-size=4            one item, value after "="
    --tp-size 4            one item, value after a space (the chart quotes
                           nothing, so the shell would split it; still seen)
    "--tp-size", "4"       two items
    --enable-metrics       a bare switch
    --no-enable-x          an explicit "off", where the engine has one
    '--json-arg={"a": 1}'  a JSON value, quotes and spaces included

A tuner needs the list as flags (`tp: 4`) and needs to write a flag back
without disturbing anything else: a flag already present is replaced where it
stands and in the spelling it was written, a new one is appended, and every
item it did not touch comes back byte for byte. That last property is what
lets a catalog file go through a tuning campaign and come out reading like the
file a person wrote.

Keys are the platform's canonical snake_case vocabulary (engines/flags.py):
aliases fold, so `--tensor-parallel-size` and `--tp` are the same flag. The
last occurrence of a repeated flag wins, as it does in argparse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.control.engines.flags import negated_flag, normalize_args, render_flag

_INT = re.compile(r"^-?\d+$")
_FLOAT = re.compile(r"^-?(\d+\.\d*|\.\d+|\d+(\.\d*)?[eE][-+]?\d+)$")


@dataclass(frozen=True)
class Arg:
    """One flag as it sits in the list: where, how spelled, and its value."""

    start: int  # index of the flag's first item
    width: int  # 1, or 2 for the two-item form
    flag: str  # as written, "--tp-size"
    raw: str | None  # the value as written; None for a bare switch
    form: str  # "eq" | "space" | "pair" | "switch"


def _key(flag: str) -> str:
    return flag.lstrip("-").replace("-", "_")


def coerce(raw: str) -> Any:
    """A written value as the type a search space would hold it in: "4" is 4,
    "0.85" is 0.85, anything else stays the string it was."""
    text = raw.strip()
    if _INT.match(text):
        return int(text)
    if _FLOAT.match(text):
        return float(text)
    return raw


def fmt(value: Any) -> str:
    """A value as a flag spells it: 2.0 from a float-snapping policy is 2."""
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return str(value)


def scan(items: list[Any]) -> list[Arg]:
    """Every flag in the list, in order. Items that are not flags (a stray
    positional) are skipped, and kept in place by every editor below."""
    out: list[Arg] = []
    texts = [str(item) for item in items]
    index = 0
    while index < len(texts):
        text = texts[index]
        if not text.startswith("--"):
            index += 1
            continue
        if "=" in text:
            flag, _, value = text.partition("=")
            out.append(Arg(index, 1, flag, value, "eq"))
            index += 1
            continue
        if " " in text:
            flag, _, value = text.partition(" ")
            out.append(Arg(index, 1, flag, value.strip(), "space"))
            index += 1
            continue
        following = texts[index + 1] if index + 1 < len(texts) else None
        if following is not None and not following.startswith("--"):
            out.append(Arg(index, 2, text, following, "pair"))
            index += 2
            continue
        out.append(Arg(index, 1, text, None, "switch"))
        index += 1
    return out


def _canonical(engine: str, flag: str, value: Any) -> tuple[str, Any]:
    args, _ = normalize_args(engine, {_key(flag): value})
    return next(iter(args.items()))


def flags(engine: str, items: list[Any] | None) -> dict[str, Any]:
    """The list as {canonical key: typed value}. A bare switch is True; an
    explicit `--no-x` on a known switch is `x: False`. Last one wins."""
    out: dict[str, Any] = {}
    for arg in scan(list(items or [])):
        key, value = _canonical(engine, arg.flag, True if arg.raw is None else arg.raw)
        if isinstance(value, str):
            value = coerce(value)
        out.pop(key, None)  # re-insert so the order is the effective order
        out[key] = value
    return out


def _positions(engine: str, items: list[Any], key: str) -> list[Arg]:
    """Every occurrence of `key`, either polarity, any alias spelling."""
    found = []
    for arg in scan(items):
        canonical, _ = _canonical(engine, arg.flag, True if arg.raw is None else arg.raw)
        if canonical == key:
            found.append(arg)
    return found


def _rendered(engine: str, key: str, value: Any, like: Arg | None) -> list[str] | None:
    """`key: value` as list items, in the spelling of `like` when given.
    None means "nothing to write" (False on a switch the engine cannot negate,
    which omission already says)."""
    positive = like is not None and not like.flag.startswith("--no-")
    if isinstance(value, bool):
        if value:
            return [like.flag if positive and like.raw is None else render_flag(engine, key)]
        negated = negated_flag(engine, key)
        return [negated] if negated is not None else None
    flag = like.flag if positive else render_flag(engine, key)
    text = fmt(value)
    form = like.form if like is not None else "eq"
    if form == "pair":
        return [flag, text]
    if form == "space":
        return [f"{flag} {text}"]
    return [f"{flag}={text}"]


def set_flag(engine: str, items: list[Any] | None, key: str, value: Any) -> list[Any]:
    """A new list with `key` set to `value` (None removes it).

    The last occurrence is rewritten in place, in its own spelling; earlier
    duplicates are dropped, since they never took effect and a reader would
    take them for the value. A flag not yet present is appended."""
    items = list(items or [])
    found = _positions(engine, items, key)
    last = found[-1] if found else None
    replacement = None if value is None else _rendered(engine, key, value, last)
    drop = {i for arg in found for i in range(arg.start, arg.start + arg.width)}
    out: list[Any] = []
    for index, item in enumerate(items):
        if last is not None and index == last.start and replacement is not None:
            out.extend(replacement)
        if index in drop:
            continue
        out.append(item)
    if last is None and replacement is not None:
        out.extend(replacement)
    return out


def render(engine: str, args: dict[str, Any]) -> list[str]:
    """A flag dict as a fresh extraArgs list, `--flag=value` per item, in the
    dict's order (the catalog's own spelling)."""
    out: list[str] = []
    for key, value in args.items():
        if value is None:
            continue
        out.extend(_rendered(engine, key, value, None) or [])
    return out
