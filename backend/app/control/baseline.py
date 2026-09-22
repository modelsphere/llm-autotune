"""Resolving the production reference a campaign is measured against.

A campaign tunes a (served model, engine, card type); the baseline for that
triple is the production config to compare candidates to. Kept here rather than
in the supervisor so the scheduler and any preflight ask the same question the
same way — and so "does production already run this config" (the in-place
shortcut) is decided by one comparison, not two that can disagree.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Baseline


def resolve_baseline(
    session: Session, served_model_name: str, engine: str, card_type: str
) -> Baseline | None:
    """The reference for exactly this (model, engine, card type), or None.

    An exact match only: a baseline measured on a different card type is not a
    reference this campaign can rank against, and silently borrowing one would
    read as comparable when it is not. A row with an empty card_type is the
    deliberate "any card" wildcard and does match.
    """
    exact = session.scalars(
        select(Baseline).where(
            Baseline.served_model_name == served_model_name,
            Baseline.engine == engine,
            Baseline.card_type == card_type,
        )
    ).first()
    if exact is not None:
        return exact
    return session.scalars(
        select(Baseline).where(
            Baseline.served_model_name == served_model_name,
            Baseline.engine == engine,
            Baseline.card_type == "",
        )
    ).first()


def _normalize(config: dict[str, Any] | None) -> dict[str, str]:
    """Config as {key: str(value)} so a parsed command (tp="2") and a hand-set
    or search config (tp=2) compare equal — both describe the same launch."""
    return {str(k): str(v) for k, v in (config or {}).items()}


def same_config(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    """Whether two engine configs would launch the same service. Both are
    expected to be placement-stripped already (baseline_engine_args does this),
    so this compares the tuning knobs only."""
    return _normalize(a) == _normalize(b)
