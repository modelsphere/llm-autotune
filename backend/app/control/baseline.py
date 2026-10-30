"""Resolving the production reference a campaign is measured against.

A campaign tunes a (served model, engine, card type); the baseline for that
triple is the production config to compare candidates to. Kept here rather than
in the supervisor so the scheduler and any preflight ask the same question the
same way — and so "does production already run this config" (the in-place
shortcut) is decided by one comparison, not two that can disagree.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.control.launch_config import LaunchConfig
from app.db.models import Baseline, Campaign


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


@dataclass(frozen=True)
class Production:
    """Production for a campaign, as the policy is told it and the platform
    validates it: one definition, so the two can never measure different
    things under the same name.

    `config` is what a policy sends to launch it: the space's base and the
    recorded Baselines args, over the campaign's anchor. `launch` is what that
    runs — for a recipe campaign, the recipe with `config` applied, read the
    way every starting config is read (LaunchConfig). `source` says where it
    came from: "baselines" (a recorded reference), "recipe" (none recorded, so
    the campaign's recipe as written) or "campaign" (neither: the campaign's
    own fields and base)."""

    config: dict[str, Any]
    launch: LaunchConfig
    source: str
    baseline_id: int | None = None


def production_for(session: Session, campaign: Campaign, card_type: str) -> Production:
    from app.control.search.space import prune_inactive

    recorded = resolve_baseline(session, campaign.served_model_name, campaign.engine, card_type)
    space = campaign.search_space or {}
    config = prune_inactive(
        {**(space.get("base") or {}), **dict((recorded.engine_args if recorded else None) or {})},
        space,
    )
    if recorded is not None:
        source = "baselines"
    else:
        source = "recipe" if campaign.recipe else "campaign"
    return Production(
        config=config,
        launch=LaunchConfig.from_campaign(campaign, config, gpu_type=card_type),
        source=source,
        baseline_id=recorded.id if recorded is not None else None,
    )
