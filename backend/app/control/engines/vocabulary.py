"""What an engine image reads: its flags and environment variables, and which
of them are worth a policy's trials.

The vocabulary is read from the image a campaign runs — keyed by the image,
dumped when it is first used — never from what this platform was built
against: a moved tag, a newer engine or a patched fork each read different
switches, and a stale list would tell a policy about knobs that do nothing.
Each entry carries the engine's own words for it where the image (or,
optionally, the engine's docs at that version) has them.

On top sit the recommendation labels in `labels/<engine>.yaml`, written by
hand: high / mid / low, everything unlabeled `unknown`. Hundreds of variables
are mostly debug, CI and other hardware; the labels are how a policy finds
the few worth trying first. A label is applied only to an entry the image
actually has, and only on hardware it names, so it can never point a policy
at a switch this engine or this card does not have.
"""

import difflib
from dataclasses import asdict, dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from app.control.engines.env_keys import ENV_PREFIX, env_name, is_env_key
from app.hardware import hardware_tags

LEVELS: tuple[str, ...] = ("high", "mid", "low", "unknown")
KINDS: tuple[str, ...] = ("flag", "env")

_LABELS_DIR = Path(__file__).parent / "labels"


@dataclass(frozen=True)
class Entry:
    kind: str  # flag | env
    name: str  # flag: canonical snake_case key; env: the variable's name
    type: str = ""  # bool | int | float | str | ... as the engine declares it
    default: Any = None
    choices: tuple[Any, ...] = ()
    # The engine takes names beyond `choices` too (a plugin registry).
    choices_open: bool = False
    help: str = ""
    # Whose words `help` is: "engine" (the image's own help or comment),
    # "docs" (the engine's docs at this version), "source" (the code that
    # reads it, when nothing else describes it).
    help_source: str = ""
    where: str = ""  # file:line in the engine where it is read
    section: str = ""  # the engine's own grouping ("DeepGEMM", "AMD & ROCm")
    # Vendor/architecture tags it only applies to, as read from its name and
    # section ("amd" for an AITER or ROCm switch); empty = any hardware.
    hardware: tuple[str, ...] = ()


@dataclass(frozen=True)
class Label:
    kind: str
    name: str
    level: str
    when: str = ""  # when it is worth trying: "MoE models", "speculative decoding on"
    # Vendor/architecture tags (app.hardware.hardware_tags) it applies to;
    # empty = any hardware.
    hardware: tuple[str, ...] = ()
    # Where to search, when the label says: a short list of values worth a
    # trial, each with the hardware it is for (empty = any), or an interval.
    values: tuple[tuple[Any, tuple[str, ...]], ...] = ()
    range: dict[str, float] | None = None


@dataclass
class Vocabulary:
    engine: str
    engine_version: str = ""
    entries: list[Entry] = field(default_factory=list)

    def names(self, kind: str) -> set[str]:
        return {e.name for e in self.entries if e.kind == kind}


def from_row(row: Any) -> Vocabulary:
    """A stored EngineVocabulary row as a Vocabulary."""
    fields = set(Entry.__dataclass_fields__)
    entries = [
        Entry(**{
            **{k: v for k, v in e.items() if k in fields},
            "choices": tuple(e.get("choices") or ()),
            "hardware": tuple(e.get("hardware") or ()),
        })
        for e in row.entries or []
    ]
    return Vocabulary(engine=row.engine, engine_version=row.engine_version or "", entries=entries)


@cache
def load_labels(engine: str) -> tuple[Label, ...]:
    """The hand-written labels for an engine; none when it has no file."""
    path = _LABELS_DIR / f"{engine}.yaml"
    if not path.exists():
        return ()
    raw = yaml.safe_load(path.read_text()) or {}
    out: list[Label] = []
    for kind in KINDS:
        for row in raw.get(kind) or []:
            level = str(row.get("level") or "unknown")
            if level not in LEVELS:
                raise ValueError(f"{path.name}: {row.get('name')!r} has level {level!r}")
            out.append(Label(
                kind=kind, name=str(row["name"]), level=level,
                when=str(row.get("when") or ""),
                hardware=tuple(str(h) for h in row.get("hardware") or ()),
                values=tuple(_value(v, path.name, row["name"]) for v in row.get("values") or ()),
                range=_range(row.get("range"), path.name, row["name"]),
            ))
    return tuple(out)


def _value(raw: Any, file: str, name: str) -> tuple[Any, tuple[str, ...]]:
    """`fa3` or `{value: fa3, hardware: [hopper]}`."""
    if isinstance(raw, dict):
        if "value" not in raw:
            raise ValueError(f"{file}: a value of {name!r} has no `value`")
        return raw["value"], tuple(str(h) for h in raw.get("hardware") or ())
    return raw, ()


def _range(raw: Any, file: str, name: str) -> dict[str, float] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or not {"min", "max"} <= set(raw):
        raise ValueError(f"{file}: the range of {name!r} needs `min` and `max`")
    if not all(isinstance(raw[k], int | float) for k in raw if k in ("min", "max", "step")):
        raise ValueError(f"{file}: the range of {name!r} must be numbers")
    if raw["min"] > raw["max"]:
        raise ValueError(f"{file}: the range of {name!r} has min above max")
    return {k: raw[k] for k in ("min", "max", "step") if k in raw}


def _suggested(label: Label, entry: Entry, tags: frozenset[str]) -> dict[str, Any] | None:
    """The label's search hint as it applies here: values for other hardware
    left out, and, when the engine declares its choices, only values it
    accepts — a stale label never suggests what this image would refuse."""
    if label.range is not None:
        return {"range": dict(label.range)}
    if not label.values:
        return None
    accepted = {str(c).lower() for c in entry.choices}
    values = [
        v for v, hw in label.values
        if (not hw or not tags or tags & set(hw))
        and (not accepted or str(v).lower() in accepted)
    ]
    return {"values": values} if values else None


def _applies(label: Label, tags: frozenset[str]) -> bool:
    # An unknown card keeps every label: better a lever that may not apply
    # than hiding the right one.
    return not label.hardware or not tags or bool(tags & set(label.hardware))


def annotate(vocab: Vocabulary, gpu_type: str = "") -> list[dict[str, Any]]:
    """Every entry with its recommendation level, best first. An entry for
    other hardware (by its label, or by what the engine says it is for) is
    left out: it cannot help here."""
    tags = hardware_tags(gpu_type)
    labels = {(lb.kind, lb.name): lb for lb in load_labels(vocab.engine)}
    out: list[dict[str, Any]] = []
    for entry in vocab.entries:
        label = labels.get((entry.kind, entry.name))
        if label is not None and not _applies(label, tags):
            continue
        if label is None and tags and entry.hardware and not tags & set(entry.hardware):
            continue
        row = asdict(entry)
        row["choices"] = list(entry.choices)
        row["hardware"] = list(entry.hardware)
        row["level"] = label.level if label else "unknown"
        row["when"] = label.when if label else ""
        row["suggested"] = _suggested(label, entry, tags) if label else None
        out.append(row)
    rank = {level: i for i, level in enumerate(LEVELS)}
    out.sort(key=lambda r: (rank[r["level"]], r["kind"], r["name"]))
    return out


def lookup(
    rows: list[dict[str, Any]], *, kind: str = "", query: str = "", level: str = "",
) -> list[dict[str, Any]]:
    """Annotated rows filtered by kind, a case-insensitive substring of the
    name or help, and the minimum level ("mid" = high and mid)."""
    rank = {lv: i for i, lv in enumerate(LEVELS)}
    q = query.lower()
    return [
        r for r in rows
        if (not kind or r["kind"] == kind)
        and (not level or rank[r["level"]] <= rank.get(level, len(LEVELS)))
        and (not q or q in r["name"].lower() or q in (r["help"] or "").lower())
    ]


def unknown_env_problems(config: dict[str, Any], vocab: Vocabulary) -> list[dict[str, Any]]:
    """An env key the image does not read, with the nearest names it does.
    The engine would ignore it silently and the trial would measure nothing."""
    known = vocab.names("env")
    problems: list[dict[str, Any]] = []
    for key in config:
        if not is_env_key(key) or env_name(key) in known:
            continue
        near = difflib.get_close_matches(env_name(key), sorted(known), n=3, cutoff=0.7)
        hint = f"; did you mean {', '.join(ENV_PREFIX + n for n in near)}?" if near else ""
        problems.append({
            "param": key, "kind": "unknown_env",
            "message": f"{env_name(key)} is not an environment variable this "
            f"{vocab.engine} image reads (the engine would ignore it){hint}",
        })
    return problems
