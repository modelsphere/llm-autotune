"""BFCL scan plans and selection shared by reports and plugins."""

from __future__ import annotations

import math
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Concurrency = Annotated[int, Field(strict=True, ge=1, le=128)]

# Matches LLMBench's BFCL scan cap (48h Dramatiq actor limit minus 1h headroom).
BFCL_SCAN_MAX_SECONDS = 47 * 3600
BFCL_SCAN_METRIC_KEYS = frozenset({
    "reported_concurrency", "sweep_complete", "sweep_canceled", "sweep_timed_out",
    "sweep_points_attempted", "sweep_points_completed", "sweep_points_planned",
    "sweep_stop_reason",
})


class BFCLSweepPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["linear"] = "linear"
    min: Concurrency
    max: Concurrency
    points: int = Field(default=5, strict=True, ge=2, le=16)

    @model_validator(mode="after")
    def validate_range(self):
        if self.max - self.min + 1 < self.points:
            raise ValueError("the range must contain at least points distinct integers")
        return self

    def resolved(self) -> list[int]:
        denominator = self.points - 1
        return [self.min + (2 * i * (self.max - self.min) + denominator) // (2 * denominator)
                for i in range(self.points)]

    def snapshot(self) -> dict:
        return {**self.model_dump(), "resolved_concurrencies": self.resolved(),
                "sampling_version": 1}


def bfcl_levels(metrics: dict, key: str) -> list[dict]:
    """Recover all points from the platform's lossless flattened metrics."""
    levels: dict[int, dict] = {}
    prefix = f"{key}."
    for name, value in metrics.items():
        if not name.startswith(prefix):
            continue
        match = re.fullmatch(r"c(\d+)\.(.+)", name[len(prefix):])
        if match:
            levels.setdefault(int(match[1]), {})[match[2]] = value
    return [{"concurrency": c, "metrics": {k: v for k, v in values.items() if k != "error"},
             "error": str(values.get("error") or "")} for c, values in sorted(levels.items())]


def _within(value: Any, low: Any, high: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and (low is None or value >= low)
        and (high is None or value <= high)
    )


def select_bfcl_level(metrics: dict, report: dict, floors: dict | None = None) -> dict | None:
    """Highest-TPM valid point clearing frozen redlines and confirmed floors.

    A diagnostic representative remains in raw scalars when no point passes;
    it must never become a best_level or a ranked row.
    """
    key = str(report.get("module") or report.get("module_name") or "bfcl_v4")
    canceled = metrics.get(f"{key}.sweep_canceled") in (1, True, "1", "true")
    status = str(report.get("status") or "").lower()
    error = str(report.get("error") or "").strip()
    if canceled or status in ("canceled", "cancelled", "aborted", "failed") or error:
        return None
    bounds = [b for b in report.get("bounds") or [] if b.get("role") == "redline"]
    wanted = {k[len(key) + 1:]: v for k, v in (floors or {}).items()
              if k.startswith(f"{key}.")}
    scan_checks: dict[str, tuple[Any, Any]] = {}
    point_checks: dict[str, tuple[Any, Any]] = {}
    for b in bounds:
        name = b["metric"]
        target = scan_checks if name in BFCL_SCAN_METRIC_KEYS else point_checks
        target[name] = (b.get("min"), b.get("max"))
    for name, floor in wanted.items():
        target = scan_checks if name in BFCL_SCAN_METRIC_KEYS else point_checks
        target[name] = (floor, None)
    reported_limits = scan_checks.pop("reported_concurrency", None)
    for name, limits in scan_checks.items():
        if not _within(metrics.get(f"{key}.{name}"), *limits):
            return None
    choices = []
    for level in bfcl_levels(metrics, key):
        values = level["metrics"]
        if values.get("valid") != 1:
            continue
        if reported_limits is not None and not _within(level["concurrency"], *reported_limits):
            continue
        if (_within(values.get("total_tpm"), None, None)
                and all(_within(values.get(name), *limits)
                        for name, limits in point_checks.items())):
            choices.append(level)
    return max(choices, key=lambda lv: (lv["metrics"]["total_tpm"], -lv["concurrency"])) \
        if choices else None
