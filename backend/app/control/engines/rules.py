"""The engine-wide rules in `labels/common.yaml`: what a config may never set
(placement, credentials) and what is forbidden by default (switches that
change answers rather than speed). Data, so that the lists are read and
reviewed in one place next to the recommendation labels."""

from functools import cache
from pathlib import Path

import yaml

_COMMON = Path(__file__).parent / "labels" / "common.yaml"


@cache
def _common() -> dict:
    raw = yaml.safe_load(_COMMON.read_text()) or {}
    for key in ("never", "forbidden"):
        values = raw.get(key) or []
        if not all(isinstance(v, str) and v.strip() for v in values):
            raise ValueError(f"{_COMMON.name}: every `{key}` entry must be a pattern string")
    return raw


def never() -> tuple[str, ...]:
    """Patterns (over config keys) no config may set, for any campaign."""
    return tuple(_common().get("never") or ())


def default_forbidden() -> tuple[str, ...]:
    """The default forbidden-parameter patterns."""
    return tuple(_common().get("forbidden") or ())
