"""A recipe from whatever describes a deployment.

    a model-catalog variant      kind: Variant (catalog.swiss/v2)
    an LLMService                kind: LLMService, e.g. `kubectl get llmsvc -o yaml`
    a recipe                     {engine, chart, values}
    a command line               `docker run ... sglang serve ...`, or the argv

An LLMService swiss wrote carries its variant as the `catalog` layer, so that
layer IS the recipe and the rest (site, form, edits) is left behind. One with
no catalog layer is read as the merge of its layers with the site's and the
run's keys taken out, and says so in `notes`.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

import yaml

from app.control.recipe.check import SITE_KEYS
from app.control.recipe.compose import effective_values
from app.control.recipe.legacy import from_fields
from app.control.recipe.model import ChartRef, Recipe


class NotARecipe(ValueError):
    pass


@dataclass
class Imported:
    recipe: Recipe
    source: str  # variant | llmservice | recipe | command
    notes: list[str] = field(default_factory=list)


def _from_llmservice(doc: dict[str, Any]) -> Imported:
    spec = doc.get("spec") or {}
    chart = spec.get("chart") or {}
    layers = [layer for layer in spec.get("layers") or [] if isinstance(layer, dict)]
    model = spec.get("model") or {}
    notes: list[str] = []
    catalog = next((layer for layer in layers if layer.get("name") == "catalog"), None)
    if catalog is not None:
        values = copy.deepcopy(catalog.get("values") or {})
    else:
        values = effective_values(
            [(str(layer.get("name")), layer.get("values") or {}) for layer in layers]
        )
        dropped = sorted(k for k in values if k in SITE_KEYS)
        for key in dropped:
            values.pop(key)
        notes.append("no catalog layer: read as the merge of every layer")
        if dropped:
            notes.append("left out as the site's or a deploy's: " + ", ".join(dropped))
    # A pinned version is what was deployed; the range what it asked for.
    pinned = ((doc.get("status") or {}).get("chart") or {}).get("version")
    version = str(chart.get("version") or "")
    if pinned and pinned != version:
        notes.append(f"chart {chart.get('name')} {version} was deployed as {pinned}")
    engine = model.get("engine") or chart.get("name") or "sglang"
    return Imported(
        Recipe(
            engine=engine,
            chart=ChartRef(name=chart.get("name") or engine, version=version),
            values=values,
        ),
        "llmservice",
        notes,
    )


def parse(source: str | dict[str, Any] | list[Any]) -> Imported:
    """Any of the forms above. Raises NotARecipe with what it could not read."""
    if isinstance(source, list):
        return _from_command(source)
    if isinstance(source, str):
        text = source.strip()
        if not text:
            raise NotARecipe("empty")
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError:
            doc = None
        if not isinstance(doc, dict):
            return _from_command(text)
    else:
        doc = source
    kind = doc.get("kind")
    if kind == "LLMService":
        return _from_llmservice(doc)
    if kind == "Variant" or (doc.get("requires") and doc.get("values") is not None):
        return Imported(Recipe.from_variant(doc), "variant")
    if "values" in doc and isinstance(doc.get("values"), dict):
        try:
            return Imported(Recipe.model_validate(doc), "recipe")
        except Exception as exc:  # pydantic's message names the field
            raise NotARecipe(f"not a recipe: {exc}") from exc
    raise NotARecipe(
        "expected a catalog variant, an LLMService, a recipe ({engine, chart, values})"
        " or a command line"
    )


def _from_command(command: str | list[Any]) -> Imported:
    from app.control.engine_command import parse_launch_command

    parsed = parse_launch_command(command)
    if not parsed.get("engine_args") and not parsed.get("image"):
        raise NotARecipe("found neither an engine command nor an image in it")
    notes = []
    if not parsed.get("image"):
        notes.append("no image in the command: set values.image")
    recipe = from_fields(
        engine=parsed.get("engine") or "sglang",
        image=parsed.get("image") or "",
        served_model_name=parsed.get("served_model_name") or "",
        engine_args=dict(parsed.get("engine_args") or {}),
        extra_env=dict(parsed.get("extra_env") or {}),
        extra_volumes=dict(parsed.get("extra_volumes") or {}),
    )
    if parsed.get("model_path"):
        notes.append(
            f"the weights were at {parsed['model_path']}: that is the campaign's model path"
        )
    return Imported(recipe, "command", notes)
