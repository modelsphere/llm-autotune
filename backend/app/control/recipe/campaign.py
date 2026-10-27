"""A campaign's configurations as recipes.

A campaign with a recipe launches `apply(campaign.recipe, candidate config)`:
the recipe is the anchor, and a candidate's config is the addresses it
changes. A campaign without one (every campaign before recipes) is built from
its fields exactly as before (legacy.py), so nothing about it changes.

Cards are counted from the recipe when there is one, because that is what the
pod asks for: a config that never mentions `tp` still runs the recipe's
`--tp-size`, and the request is `model.gpus` per pod.
"""

from __future__ import annotations

from typing import Any

from app.control.recipe.convert import apply
from app.control.recipe.legacy import from_fields
from app.control.recipe.model import Recipe
from app.control.recipe.shape import shape
from app.control.search.validation import cards_used


def base_recipe(campaign: Any) -> Recipe | None:
    raw = getattr(campaign, "recipe", None) or None
    return Recipe.model_validate(raw) if raw else None


def split_flat(config: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """(flags and env keys, the other addresses): what every driver can apply,
    and what only a recipe can carry (pd role flags, chart values)."""
    flat: dict[str, Any] = {}
    rest: dict[str, Any] = {}
    for key, value in (config or {}).items():
        is_flat = "." not in key or key.startswith("env.")
        (flat if is_flat else rest)[key] = value
    return flat, rest


def recipe_for(campaign: Any, config: dict[str, Any] | None) -> Recipe:
    base = base_recipe(campaign)
    if base is not None:
        return apply(base, dict(config or {}))
    flat, rest = split_flat(config)
    built = from_fields(
        engine=campaign.engine,
        image=campaign.image,
        served_model_name=campaign.served_model_name,
        engine_args=flat,
        extra_env=dict(campaign.extra_env or {}),
        extra_volumes=dict(campaign.extra_volumes or {}),
    )
    return apply(built, rest) if rest else built


def config_cards(campaign: Any, config: dict[str, Any] | None) -> int:
    """Cards one launch of `config` claims."""
    base = base_recipe(campaign)
    if base is None:
        return cards_used(dict(config or {}))
    return shape(apply(base, dict(config or {}))).total_gpus


def config_pod_cards(campaign: Any, config: dict[str, Any] | None) -> int:
    """Cards the widest pod of one launch of `config` claims: what one node
    must hold. The whole launch for a one-pod config; one pod of a gang or a
    pd role otherwise, since those spread over several nodes."""
    base = base_recipe(campaign)
    if base is None:
        return cards_used(dict(config or {}))
    return shape(apply(base, dict(config or {}))).max_pod_gpus


def widest_cards(campaign: Any) -> int:
    """The most cards any configuration of the campaign's space claims."""
    from app.control.search.space import expand
    from app.control.search.validation import widest_cards as flag_widest

    base = base_recipe(campaign)
    space = getattr(campaign, "search_space", None) or {}
    if base is None:
        return flag_widest(space)
    return max((config_cards(campaign, c) for c in expand(space)), default=base.gpus)
