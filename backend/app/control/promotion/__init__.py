"""Promotion targets — the registry, mirroring the launch driver registry.

A campaign winner is handed to the target named by `promotion_target`
(default "manual"). Add a substrate by implementing PromotionTarget and
registering it here; nothing above this package names a concrete target.
"""

from app.control.promotion.base import (
    TERMINAL_PROMOTION_STATES,
    PromotionError,
    PromotionHandle,
    PromotionRequest,
    PromotionState,
    PromotionStatus,
    PromotionTarget,
    PromotionUnavailable,
)
from app.control.promotion.config import build_promotion_config
from app.control.promotion.gitlab import GitLabPromotionTarget
from app.control.promotion.manual import ManualPromotionTarget
from app.core.config import get_settings

TARGET_REGISTRY: dict[str, type[PromotionTarget]] = {
    "manual": ManualPromotionTarget,
    "gitlab": GitLabPromotionTarget,
}


def get_target(name: str = "") -> PromotionTarget:
    """The promotion target by name, or the platform default when unnamed."""
    resolved = name or get_settings().promotion_target or "manual"
    try:
        return TARGET_REGISTRY[resolved]()
    except KeyError as exc:
        raise ValueError(
            f"unknown promotion target '{resolved}' (available: {list(TARGET_REGISTRY)})"
        ) from exc


__all__ = [
    "TARGET_REGISTRY",
    "TERMINAL_PROMOTION_STATES",
    "GitLabPromotionTarget",
    "ManualPromotionTarget",
    "PromotionError",
    "PromotionHandle",
    "PromotionRequest",
    "PromotionState",
    "PromotionStatus",
    "PromotionTarget",
    "PromotionUnavailable",
    "build_promotion_config",
    "get_target",
]
