"""The Datasets page's form, and the LLMBench collection profile it becomes.

The page asks for what a person knows: the model, perhaps the nodes serving
it, how many requests, and how often to take a new sample. Every other field of
a collection profile gets a default that suits most traffic, set here once.
The profile reads from a VictoriaLogs store of the gateway's request logs.
"""

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

VICTORIALOGS = "victorialogs"
DESCRIPTION = "Set up on LLM AutoTune's Datasets page."

# The defaults behind the form's Advanced section, in LLMBench's own names.
DEFAULT_STATUSES = ["200"]
DEFAULT_URIS = ["/v1/chat/completions"]

_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class DatasetForm(BaseModel):
    """What the page sends. Field names follow LLMBench's profile, except
    `model` (one served model per dataset) and `refresh_hours` (kept here)."""

    model: str = Field(min_length=1, max_length=200)
    # Backends serving it: host, host:port or a URL. A host matches every port.
    # Empty = every backend serving the model.
    forwarded_to: list[str] = Field(default_factory=list)
    sample_size: int = Field(2000, ge=1, le=1_000_000)
    window_hours: int = Field(24, ge=1, le=24 * 30)
    # 0 = only when asked.
    refresh_hours: int = Field(24, ge=0, le=24 * 30)
    display_name: str = Field("", max_length=200)
    # The profile's name on LLMBench, which cannot change. Derived when empty.
    name: str = Field("", max_length=64)

    # Advanced
    source_url: str = Field("", max_length=500)
    statuses: list[str] = Field(default_factory=lambda: list(DEFAULT_STATUSES))
    uris: list[str] = Field(default_factory=lambda: list(DEFAULT_URIS))
    extra_logsql: str = Field("", max_length=2000)
    max_model_len: int = Field(262144, ge=2048)
    min_records: int = Field(100, ge=1)
    keep_builds: int = Field(7, ge=1, le=100)
    subwindow_minutes: int = Field(60, ge=1, le=24 * 60)
    max_carry_multiple: int = Field(4, ge=1, le=10_000)
    window_timezone: str = Field("UTC", min_length=1, max_length=64)
    # sampled: single requests spread over the window. timed: every request of
    # one stretch at typical load, in time order with sessions, for a 1×
    # replay; sample_size is then how many requests the stretch holds.
    layout: Literal["sampled", "timed"] = "sampled"
    # Where the logs keep a session id (dotted field); empty = message prefixes.
    session_field: str = Field("", max_length=200)
    max_idle_gap_s: float = Field(5.0, ge=0.0, le=3600.0)

    @field_validator("model", "display_name", "name", "source_url", "extra_logsql")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("forwarded_to", "statuses", "uris")
    @classmethod
    def _clean(cls, v: list[str]) -> list[str]:
        return [item.strip() for item in v if isinstance(item, str) and item.strip()]


def slug_for(text: str) -> str:
    """A profile name from a display name or a model: lowercase letters,
    digits, '-' and '_', at most 64, starting with a letter or digit."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", text.lower()).strip("-_")
    slug = re.sub(r"-{2,}", "-", slug)[:64].rstrip("-_")
    return slug or "dataset"


def unused_slug(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    for n in range(2, 1000):
        candidate = f"{base[:60]}-{n}"
        if candidate not in taken:
            return candidate
    raise ValueError(f"no free name like {base!r}")


def valid_slug(name: str) -> bool:
    return bool(_SLUG.match(name))


def profile_fields(form: DatasetForm, source_url: str) -> dict[str, Any]:
    """Everything but the name: what a create sends and what a save sends.

    `schedule_interval_hours` is 0 because this platform takes new samples
    itself (app/datasets/refresh.py), and `max_age_hours` 0 because a sample
    kept until a campaign finishes is old by design, not stale."""
    return {
        "display_name": form.display_name or form.model,
        "description": DESCRIPTION,
        "source_type": VICTORIALOGS,
        "source_url": source_url,
        "models": [form.model],
        "forwarded_to": form.forwarded_to,
        "statuses": form.statuses,
        "uris": form.uris,
        "extra_logsql": form.extra_logsql or None,
        "window_hours": form.window_hours,
        "window_timezone": form.window_timezone,
        "subwindow_minutes": form.subwindow_minutes,
        "sample_size": form.sample_size,
        "max_carry_multiple": form.max_carry_multiple,
        "max_model_len": form.max_model_len,
        "min_records": form.min_records,
        "keep_builds": form.keep_builds,
        "schedule_interval_hours": 0,
        "max_age_hours": 0,
        # Sent only for a timed dataset, so a sampled one's profile is what it
        # always was (and an LLMBench without timed datasets is not asked).
        **({"layout": "timed", "session_field": form.session_field,
            "max_idle_gap_s": form.max_idle_gap_s} if form.layout == "timed" else {}),
    }


def filters_of(form: DatasetForm, source_url: str, hours: float) -> dict[str, Any]:
    """The source and filters of an unsaved form, for LLMBench's probe."""
    return {
        "source_type": VICTORIALOGS,
        "source_url": source_url,
        "models": [form.model],
        "forwarded_to": form.forwarded_to,
        "statuses": form.statuses,
        "uris": form.uris,
        "extra_logsql": form.extra_logsql or None,
        "hours": hours,
    }


def form_of(profile: dict[str, Any], refresh_hours: int) -> dict[str, Any]:
    """A profile read back as the form's fields, for editing it."""
    models = profile.get("models") or []
    return {
        "model": models[0] if models else "",
        "forwarded_to": list(profile.get("forwarded_to") or []),
        "sample_size": profile.get("sample_size", 2000),
        "window_hours": profile.get("window_hours", 24),
        "refresh_hours": refresh_hours,
        "display_name": profile.get("display_name") or "",
        "name": profile.get("name") or "",
        "source_url": profile.get("source_url") or "",
        "statuses": list(profile.get("statuses") or DEFAULT_STATUSES),
        "uris": list(profile.get("uris") or DEFAULT_URIS),
        "extra_logsql": profile.get("extra_logsql") or "",
        "max_model_len": profile.get("max_model_len", 262144),
        "min_records": profile.get("min_records", 100),
        "keep_builds": profile.get("keep_builds", 7),
        "subwindow_minutes": profile.get("subwindow_minutes", 60),
        "max_carry_multiple": profile.get("max_carry_multiple", 4),
        "window_timezone": profile.get("window_timezone") or "UTC",
        "layout": profile.get("layout") or "sampled",
        "session_field": profile.get("session_field") or "",
        "max_idle_gap_s": profile.get("max_idle_gap_s", 5.0),
    }
