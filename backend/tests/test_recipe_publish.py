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
from app.control.catalog_export import (
    CatalogModel,
    CatalogOptions,
    VariantSource,
    build_catalog_entry,
)
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


# -- a published variant submitted back (model-catalog PR #73) -------------------

ASCEND = VARIANTS / "deepseek-v4.1-flash-1017-w8a8__vllm-dp2-tp8-910b4-2node.yaml"
ASCEND_META = """\
apiVersion: catalog.swiss/v2
name: deepseek-v4.1-flash-1017-w8a8
displayName: DeepSeek-V4.1-Flash-1017-w8a8
description: DeepSeek-V4.1-Flash-1017, w8a8 for Ascend, on two Atlas 800T A2 (910B4) nodes.
family: deepseek
quantization: w8a8
tags:
  - chat
  - reasoning
  - tool-use
defaultVariant: vllm-dp2-tp8-910b4-2node
source:
  # A ModelScope repo id; the build is not on Hugging Face.
  hf: Eco-Tech/DeepSeek-V4.1-Flash-1017-w8a8
"""


def _ascend(port: int = 8000) -> tuple[dict, Recipe]:
    variant = yaml.safe_load(ASCEND.read_text())
    recipe = Recipe.from_variant(variant)
    if port != 8000:
        values = recipe.values
        values["service"] = {**(values.get("service") or {}), "port": port}
        script = values["commandOverride"][2]
        values["commandOverride"][2] = script.replace("--port 8000", f"--port {port}")
    return variant, recipe


def _ascend_source(recipe: Recipe, role: str = "baseline", value: float = 100.0):
    config = LaunchConfig(
        engine="vllm",
        image=recipe.image,
        served_model_name=recipe.served_name,
        engine_args={"tensor_parallel_size": 8, "data_parallel_size": 2},
        gpu_type="ASCEND-910B4-1",
    )
    return VariantSource(
        config,
        role=role,
        recipe=recipe,
        image_digest="sha256:" + "7" * 64,
        evidence={"metric": "tps", "value": value, "benchmark": "b"},
    )


def _ascend_catalog(variant: dict) -> CatalogModel:
    return CatalogModel(
        name="deepseek-v4.1-flash-1017-w8a8",
        metadata=yaml.safe_load(ASCEND_META),
        metadata_text=ASCEND_META,
        variants={variant["id"]: {"version": variant["version"], "document": variant}},
    )


def _ascend_opts(variant: dict, **kw) -> CatalogOptions:
    return CatalogOptions(
        name="deepseek-v4.1-flash-1017-w8a8", existing=_ascend_catalog(variant), **kw
    )


def test_cards_requested_under_resources_are_counted_per_node():
    _, recipe = _ascend(port=18000)
    out = build_catalog_entry(
        [_ascend_source(recipe)],
        CatalogOptions(
            name="deepseek-v4.1-flash-1017-w8a8", hf="Eco-Tech/DeepSeek-V4.1-Flash-1017-w8a8"
        ),
    )
    (doc,) = [v.document for v in out.variants]
    assert doc["requires"]["gpus"] == 0, "the recipe's 0 (swiss#41) is published as it is"
    assert "eight ASCEND-910B4-1 cards per node, 2 nodes" in doc["description"]
    assert " 0 " not in doc["description"] and "GPU" not in doc["description"]
    assert doc["id"] == "vllm-tp8-dp2-ascend-910b4-1-lws-baseline"


def test_a_multi_line_command_is_a_literal_block_and_a_pinned_tag_gets_no_second_digest():
    _, recipe = _ascend(port=18000)
    out = build_catalog_entry(
        [_ascend_source(recipe)],
        CatalogOptions(
            name="deepseek-v4.1-flash-1017-w8a8", hf="Eco-Tech/DeepSeek-V4.1-Flash-1017-w8a8"
        ),
    )
    (file,) = [f for f in out.files if f.kind == "variant"]
    assert "    - |\n      set -euo pipefail\n" in file.text
    assert "\\n" not in file.text
    assert "        --host 0.0.0.0 --port 18000 \\\n" in file.text
    image = out.variants[0].document["image"]
    assert image["tag"].startswith("pinned@sha256:") and "digest" not in image
    assert not any("release tag" in w for w in out.warnings), "a digest in the tag cannot move"
    assert yaml.safe_load(file.text) == out.variants[0].document


def test_the_published_variant_measured_again_is_that_variant_and_nothing_is_written():
    variant, recipe = _ascend()
    out = build_catalog_entry([_ascend_source(recipe)], _ascend_opts(variant))
    assert out.errors == [], out.errors
    (v,) = out.variants
    assert (v.id, v.status, v.version) == ("vllm-dp2-tp8-910b4-2node", "unchanged", "1.0.2")
    assert [f.kind for f in out.files] == ["metadata"]
    assert out.files[0].text == ASCEND_META, "nothing changed, byte for byte"


def test_a_site_port_change_is_named_as_a_near_copy_of_the_published_variant():
    variant, recipe = _ascend(port=18000)
    out = build_catalog_entry([_ascend_source(recipe)], _ascend_opts(variant))
    (warning,) = [w for w in out.warnings if "the same as the published" in w]
    assert "vllm-dp2-tp8-910b4-2node but for commandOverride, service.port" in warning
    assert out.variants[0].status == "new"


def test_an_untuned_variant_does_not_take_the_published_default():
    variant, recipe = _ascend(port=18000)
    new_id = "vllm-tp8-dp2-ascend-910b4-1-lws-baseline"
    out = build_catalog_entry(
        [_ascend_source(recipe)], _ascend_opts(variant, default_variant=new_id)
    )
    assert any("no tuning result names it" in e for e in out.errors)
    assert out.metadata["defaultVariant"] == "vllm-dp2-tp8-910b4-2node"
    # Without the request, the published default simply stays and so does the file.
    out = build_catalog_entry([_ascend_source(recipe)], _ascend_opts(variant))
    assert out.errors == [], out.errors
    (meta,) = [f for f in out.files if f.kind == "metadata"]
    assert meta.text == ASCEND_META


def test_a_tuning_result_is_added_to_the_published_metadata_without_touching_the_rest():
    variant, base = _ascend()
    tuned = apply(base, {"max_num_seqs": 64})
    out = build_catalog_entry(
        [_ascend_source(base), _ascend_source(tuned, "optimized", 120.0)],
        _ascend_opts(variant, report_file="ds-report.html"),
    )
    (meta,) = [f for f in out.files if f.kind == "metadata"]
    assert meta.text.startswith(ASCEND_META.split("defaultVariant")[0])
    assert "  # A ModelScope repo id; the build is not on Hugging Face.\n" in meta.text
    assert yaml.safe_load(meta.text) == out.metadata
    assert out.metadata["tuning"][0]["baseline"] == "vllm-dp2-tp8-910b4-2node@1.0.2"
