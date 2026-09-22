"""Deploy formats — the registry, plus the presets a binding can start from.

A binding's `format` is `{"adapter": <name>, "options": {...}}`. `get_format`
resolves it; `PRESETS` are ready-made specs for repos we know, so binding a
baseline to `llm-serving-deploy` is one click and a different values layout is
a new preset (data), not a new adapter (code).
"""

from __future__ import annotations

from typing import Any

from app.control.deploy_format.base import ChangePlan, DeployFormat, KnobChange, ParsedFile
from app.control.deploy_format.values_yaml import DEFAULT_CHART_OWNED, ValuesYamlFormat

FORMAT_REGISTRY: dict[str, type[DeployFormat]] = {
    ValuesYamlFormat.name: ValuesYamlFormat,
}

PRESETS: dict[str, dict[str, Any]] = {
    # A Helm values file on a release branch: config/model.yaml on a
    # release/<model>-<card>-<engine> branch, extraArgs as "--flag=value".
    "llm-serving-deploy": {
        "label": "llm-serving-deploy (config/model.yaml)",
        "path": "config/model.yaml",
        "adapter": "values_yaml",
        "options": dict(ValuesYamlFormat.default_options),
    },
}

DEFAULT_PRESET = "llm-serving-deploy"


def get_format(spec: dict[str, Any] | None) -> DeployFormat:
    """The adapter a binding names, with its options. An empty spec is the
    default preset."""
    spec = spec or {}
    preset = PRESETS.get(spec.get("preset") or "") if spec.get("preset") else None
    if not spec.get("adapter") and preset is None:
        preset = PRESETS[DEFAULT_PRESET]
    adapter = spec.get("adapter") or (preset or {}).get("adapter") or ValuesYamlFormat.name
    options = dict((preset or {}).get("options") or {})
    options.update(spec.get("options") or {})
    try:
        cls = FORMAT_REGISTRY[adapter]
    except KeyError as exc:
        raise ValueError(
            f"unknown deploy format adapter {adapter!r} (available: {list(FORMAT_REGISTRY)})"
        ) from exc
    return cls(options)


def chart_owned_of(fmt: DeployFormat) -> frozenset[str]:
    return frozenset(fmt.options.get("chart_owned_flags") or DEFAULT_CHART_OWNED)


def catalog() -> dict[str, Any]:
    """Adapters, presets and shortcuts, as the UI shows them."""
    from app.control.deploy_format.policy import DEFAULT_FIELD_POLICY, POLICY_FIELDS, SHORTCUTS

    return {
        "adapters": [
            {"name": name, "options": dict(cls.default_options)}
            for name, cls in FORMAT_REGISTRY.items()
        ],
        "presets": [
            {
                "name": name,
                "label": p.get("label", name),
                "path": p.get("path", ""),
                "adapter": p.get("adapter", ""),
                "options": p.get("options") or {},
            }
            for name, p in PRESETS.items()
        ],
        "default_preset": DEFAULT_PRESET,
        "fields": list(POLICY_FIELDS),
        "default_policy": dict(DEFAULT_FIELD_POLICY),
        "shortcuts": [
            {"name": name, "label": s["label"], "hint": s.get("hint", "")}
            for name, s in SHORTCUTS.items()
        ],
    }


__all__ = [
    "ChangePlan",
    "DeployFormat",
    "FORMAT_REGISTRY",
    "KnobChange",
    "PRESETS",
    "ParsedFile",
    "ValuesYamlFormat",
    "catalog",
    "chart_owned_of",
    "get_format",
]
