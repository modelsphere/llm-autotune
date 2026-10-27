"""A measured recipe is published as it is.

The variant a run produces is the recipe it launched: its values are not
added to, trimmed or rewritten, so what a deploy renders from the catalog is
what the platform measured. Only what a publisher decides is added: the id,
the description, the cards it fits and the image digest the run pulled."""

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi import HTTPException

from app.api.catalog_export import _launched_recipe
from app.control.catalog_export import CatalogOptions, VariantSource, build_catalog_entry
from app.control.launch_config import LaunchConfig
from app.control.recipe import Recipe, apply

VARIANTS = Path(__file__).parent / "data" / "catalog_variants"
QWEN = VARIANTS / "qwen3.6-35b-a3b__sglang-tp2-h100.yaml"
PD = VARIANTS / "deepseek-v4-flash-0731-nvfp4__sglang-tp8-pd-6000d.yaml"
DIGEST = "sha256:" + "a" * 64


def _source(recipe: Recipe, role: str = "optimized", value: float = 125.0) -> VariantSource:
    config = LaunchConfig(
        engine=recipe.engine,
        image=recipe.image,
        served_model_name=recipe.served_name,
        engine_args=recipe.flags(),
        gpu_type="H100",
    )
    return VariantSource(
        config,
        role=role,
        recipe=recipe,
        image_digest=DIGEST,
        evidence={"metric": "tps", "value": value, "benchmark": "b"},
    )


def _opts() -> CatalogOptions:
    return CatalogOptions(
        name="qwen3.6-35b-a3b", hf="Qwen/Qwen3.6-35B-A3B", report_file="q-report.html"
    )


@pytest.mark.parametrize("path", [QWEN, PD], ids=lambda p: p.stem)
def test_the_published_variant_is_the_recipe(path):
    variant = yaml.safe_load(path.read_text())
    tuned = apply(Recipe.from_variant(variant), {"mem_fraction_static": 0.81})
    out = build_catalog_entry([_source(tuned)], _opts())
    assert out.errors == [], out.errors
    (doc,) = [v.document for v in out.variants]
    # The values, exactly: nothing the export would once have added or dropped.
    assert doc["values"] == tuned.to_variant()["values"]
    assert doc["chart"] == variant["chart"]
    assert doc["requires"]["topology"] == variant["requires"].get("topology", "single-node")
    assert doc["image"]["digest"] == DIGEST
    # And it reads back as the recipe that was measured.
    back = Recipe.from_variant(doc, model_name=doc["model"])
    assert back.hash() == tuned.hash()


def test_a_baseline_and_its_tuned_recipe_are_two_variants_with_the_uplift():
    base = Recipe.from_variant(yaml.safe_load(QWEN.read_text()))
    tuned = apply(base, {"mem_fraction_static": 0.81})
    out = build_catalog_entry(
        [_source(base, "baseline", 100.0), _source(tuned, "optimized", 125.0)], _opts()
    )
    assert out.errors == []
    assert [v.role for v in out.variants] == ["baseline", "optimized"]
    assert out.metadata["tuning"][0]["uplift"] == 25.0


def _run(command: str) -> SimpleNamespace:
    return SimpleNamespace(id=7, launch_command=command)


def test_only_the_measured_recipe_is_published():
    recipe = Recipe.from_variant(yaml.safe_load(QWEN.read_text()))
    campaign = SimpleNamespace(recipe=recipe.model_dump(), search_space={})
    candidate = SimpleNamespace(config={"mem_fraction_static": 0.81})
    measured = apply(recipe, candidate.config).hash()
    launched = f"kubectl apply ...\n    autotune.modelsphere.dev/recipe-hash: {measured}\n"
    assert _launched_recipe(campaign, candidate, _run(launched)).hash() == measured
    # The campaign's recipe changed after the run: refused, not republished.
    campaign.recipe = apply(recipe, {"chunked_prefill_size": 4096}).model_dump()
    with pytest.raises(HTTPException) as exc:
        _launched_recipe(campaign, candidate, _run(launched))
    assert exc.value.status_code == 409


def test_a_deprecated_drivers_run_keeps_the_conversion():
    campaign = SimpleNamespace(recipe={}, search_space={})
    candidate = SimpleNamespace(config={"tp": 2})
    assert _launched_recipe(campaign, candidate, _run("docker run ... sglang serve")) is None
