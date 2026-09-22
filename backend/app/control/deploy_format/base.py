"""Deploy formats — how a production config file is read and edited.

The deploy repo's file is not the platform's model, and it can change shape
without asking. So the platform never knows a file layout; a *format adapter*
does, and the binding on a baseline names which adapter and with which
options. The contract mirrors the driver and engine registries:

    parse(text)                 -> ParsedFile   the file in LaunchConfig terms,
                                                plus where each knob sits
    apply(text, parsed, plan)   -> str          the file with a ChangePlan
                                                applied, nothing else touched

Everything between the two — comparing the file to a config under an
ownership policy, deciding what to change — is format-independent and lives
in policy.py. An adapter only has to know where things are and how they are
spelled; the first one (values_yaml) even takes that as options, so a new
Helm-values layout is a data change. A file whose encoding no option can
describe (args inside a shell string in a ConfigMap, say) is a new adapter.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class KnobSite:
    """Where one knob sits in the file — enough for the adapter to rewrite or
    delete it in place. Fields beyond `flag` are adapter-private."""

    flag: str  # the spelling as written ("--tp-size")
    value: Any  # True for a bare switch, else the text
    line: int
    indent: str = ""
    quote: str = ""
    value_line: int | None = None
    value_quote: str = ""


@dataclass
class ParsedFile:
    """A deploy file in the platform's terms."""

    engine_args: dict[str, Any] = field(default_factory=dict)  # canonical, chart-owned stripped
    image: str = ""  # repository:tag or repository@digest
    image_repository: str = ""
    image_tag: str = ""
    image_digest: str = ""
    model_path: str = ""
    served_model_name: str = ""
    service_port: int = 0
    gpus: int | None = None
    env: dict[str, str] = field(default_factory=dict)
    gpu_product: str = ""
    sites: dict[str, KnobSite] = field(default_factory=dict)  # canonical key → site
    chart_owned_present: list[str] = field(default_factory=list)
    normalized: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_launch_dict(self, engine: str = "sglang") -> dict[str, Any]:
        return {
            "engine": engine,
            "image": self.image,
            "model_path": self.model_path,
            "served_model_name": self.served_model_name,
            "service_port": self.service_port,
            "engine_args": dict(self.engine_args),
            "extra_env": dict(self.env),
            "extra_volumes": {},
        }


@dataclass
class KnobChange:
    key: str
    kind: str  # changed | added | removed
    flag: str = ""  # the spelling that will be written
    before: Any = None
    after: Any = None
    note: str = ""


@dataclass
class ChangePlan:
    """What will be written, and — just as important — what will not."""

    changes: list[KnobChange] = field(default_factory=list)
    # Production knobs the target never mentions (platform-owned, not applied
    # unless asked): absence in a search space is not a decision.
    kept: list[KnobChange] = field(default_factory=list)
    # Differences in repo-owned fields/knobs: reported, never written.
    reported: list[KnobChange] = field(default_factory=list)
    # Differences in ignored / substrate fields: listed, no warning.
    ignored: list[KnobChange] = field(default_factory=list)
    gpus_before: int | None = None
    gpus_after: int | None = None
    image_tag_before: str = ""
    image_tag_after: str = ""
    model_path_before: str = ""
    model_path_after: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return (
            not self.changes
            and self.gpus_after is None
            and not self.image_tag_after
            and not self.model_path_after
        )

    def payload(self) -> dict[str, Any]:
        def row(c: KnobChange) -> dict[str, Any]:
            return {
                "key": c.key,
                "kind": c.kind,
                "flag": c.flag,
                "before": c.before,
                "after": c.after,
                "note": c.note,
            }

        return {
            "changes": [row(c) for c in self.changes],
            "kept": [row(c) for c in self.kept],
            "reported": [row(c) for c in self.reported],
            "ignored": [row(c) for c in self.ignored],
            "gpus": {"before": self.gpus_before, "after": self.gpus_after}
            if self.gpus_after is not None
            else None,
            "image_tag": {"before": self.image_tag_before, "after": self.image_tag_after}
            if self.image_tag_after
            else None,
            "model_path": {"before": self.model_path_before, "after": self.model_path_after}
            if self.model_path_after
            else None,
            "warnings": list(self.warnings),
            "empty": self.empty,
        }


class DeployFormat(ABC):
    name: str = "base"
    #: The adapter's default options; a binding's `format.options` overrides
    #: key by key.
    default_options: dict[str, Any] = {}

    def __init__(self, options: dict[str, Any] | None = None):
        self.options = _merge(self.default_options, options or {})

    @abstractmethod
    def parse(self, text: str, engine: str = "sglang") -> ParsedFile:
        """Read the file. Never raises on an unexpected shape: it reports."""

    @abstractmethod
    def apply(self, text: str, parsed: ParsedFile, plan: ChangePlan) -> str:
        """The file with the plan applied. `parsed` came from this `text`."""


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def unified_diff(before: str, after: str, path: str = "config/model.yaml") -> str:
    import difflib

    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
