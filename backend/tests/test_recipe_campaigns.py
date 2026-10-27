"""Campaigns that start from a recipe.

A recipe campaign's runs launch the recipe with their config applied, cards
are counted from it, and a campaign is created from a catalog variant, an
LLMService or a command line as easily as from a recipe."""

from itertools import count
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
import yaml
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.control.recipe import Recipe
from app.control.recipe.campaign import config_cards, recipe_for, widest_cards
from app.control.recipe.importer import NotARecipe, parse
from app.core.auth import create_token
from app.db.base import Base, get_async_session
from app.db.models import Campaign, User
from app.main import app

VARIANTS = Path(__file__).parent / "data" / "catalog_variants"
QWEN = VARIANTS / "qwen3.6-35b-a3b__sglang-tp2-h100.yaml"
KIMI_PP = VARIANTS / "kimi-k3__sglang-tp8-pp4-6000d.yaml"


# -- reading a recipe from what describes a deployment --------------------------


def test_a_variant_is_read_as_its_recipe():
    imported = parse(QWEN.read_text())
    assert imported.source == "variant"
    assert imported.recipe.flags()["tp"] == 2
    assert imported.recipe.served_name == "qwen"


def test_an_llmservice_is_read_as_its_catalog_layer():
    variant_recipe = Recipe.from_variant(yaml.safe_load(QWEN.read_text()))
    obj = {
        "apiVersion": "serving.modelsphere.dev/v1alpha1",
        "kind": "LLMService",
        "metadata": {"name": "qwen", "namespace": "models"},
        "spec": {
            "chart": {"name": "sglang", "repo": "oci://r", "version": ">=0.8.0"},
            "layers": [
                {"name": "defaults", "values": {"cart": {"enabled": True}}},
                {"name": "catalog", "values": variant_recipe.values},
                {"name": "site", "values": {"model": {"localPath": "/data/q"}}},
                {"name": "form", "values": {"replicaCount": 3}},
            ],
            "model": {"engine": "sglang"},
        },
        "status": {"chart": {"version": "0.9.26"}},
    }
    imported = parse(yaml.safe_dump(obj))
    assert imported.source == "llmservice"
    assert imported.recipe.hash() == variant_recipe.hash()
    assert any("0.9.26" in note for note in imported.notes)


def test_an_llmservice_without_a_catalog_layer_leaves_the_site_out():
    obj = {
        "kind": "LLMService",
        "spec": {
            "chart": {"name": "vllm", "repo": "oci://r", "version": "0.8.22"},
            "layers": [
                {
                    "name": "all",
                    "values": {
                        "image": {"repository": "vllm/vllm-openai", "tag": "v0.11"},
                        "extraArgs": ["--tensor-parallel-size=2"],
                        "nodeSelector": {"a": "b"},
                        "replicaCount": 2,
                    },
                }
            ],
        },
    }
    imported = parse(obj)
    assert "nodeSelector" not in imported.recipe.values
    assert "replicaCount" not in imported.recipe.values
    assert imported.recipe.flags() == {"tp": 2}


def test_a_command_line_is_read_too():
    imported = parse(
        "docker run --gpus all -v /data/m:/model lmsysorg/sglang:v0.5.20 "
        "python3 -m sglang.launch_server --model-path /model --tp-size 4 --served-model-name q"
    )
    assert imported.source == "command"
    assert imported.recipe.image == "lmsysorg/sglang:v0.5.20"
    assert imported.recipe.flags()["tp"] == 4


def test_nothing_recognizable_is_refused():
    with pytest.raises(NotARecipe):
        parse({"hello": "world"})


# -- a recipe campaign ------------------------------------------------------------


def _campaign(recipe: Recipe | None, space=None, **fields):
    base = dict(
        engine="sglang",
        image="img:t",
        served_model_name="m",
        extra_env={},
        extra_volumes={},
        search_space=space or {},
    )
    base.update(fields)
    return SimpleNamespace(recipe=recipe.model_dump() if recipe else {}, **base)


def test_a_run_is_the_recipe_with_its_config_applied():
    recipe = Recipe.from_variant(yaml.safe_load(QWEN.read_text()))
    run = recipe_for(_campaign(recipe), {"mem_fraction_static": 0.8, "env.SGLANG_X": "1"})
    assert run.flags()["mem_fraction_static"] == 0.8
    assert run.flags()["tp"] == 2  # the recipe's, untouched
    assert run.env()["SGLANG_X"] == "1"


def test_cards_come_from_the_recipe_not_the_config():
    recipe = Recipe.from_variant(yaml.safe_load(KIMI_PP.read_text()))
    campaign = _campaign(recipe, {"grid": {"mem_fraction_static": [0.8, 0.85]}})
    # The config never says tp or pp; the recipe's group is 4 pods of 8.
    assert config_cards(campaign, {"mem_fraction_static": 0.8}) == 32
    assert widest_cards(campaign) == 32
    # Without a recipe, the flags decide, as always.
    assert config_cards(_campaign(None), {"tp": 4}) == 4


# -- created through the API -------------------------------------------------------

_counter = count()


@pytest_asyncio.fixture
async def client():
    name = f"recipe_campaigns_{next(_counter)}"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///file:{name}?mode=memory&cache=shared&uri=true"
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _session():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_async_session] = _session
    async with factory() as session:
        user = User(id=1, username="admin", password_hash="x", role="admin")
        session.add(user)
        await session.commit()
        token = create_token(user)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
        http.headers["Authorization"] = f"Bearer {token}"
        http.db = factory
        yield http
    app.dependency_overrides.clear()
    await engine.dispose()


def _body(**extra):
    return {"name": "c", "model_path": "/data/qwen", "search_space": {}, **extra}


@pytest.mark.asyncio
async def test_a_campaign_from_a_variant_takes_its_engine_image_and_name(client):
    resp = await client.post(
        "/api/campaigns",
        json=_body(
            recipe_text=QWEN.read_text(),
            search_space={"grid": {"mem_fraction_static": [0.8, 0.85], "prefill.page_size": [64]}},
        ),
    )
    # prefill.* addresses a pd role; this recipe has none, but the address is valid.
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["engine"] == "sglang"
    assert out["image"] == "lmsysorg/sglang:v0.5.18-cu129"
    assert out["served_model_name"] == "qwen"
    assert out["recipe"]["values"]["extraArgs"][0] == "--tp-size=2"
    async with client.db() as session:
        campaign = await session.get(Campaign, out["id"])
        assert Recipe.model_validate(campaign.recipe).flags()["tp"] == 2


@pytest.mark.asyncio
async def test_a_recipe_campaign_refuses_what_is_not_an_address(client):
    resp = await client.post(
        "/api/campaigns",
        json=_body(
            recipe_text=QWEN.read_text(),
            search_space={"grid": {"values.nodeSelector.zone": ["a"], "pd.prefill.replicas": [1]}},
        ),
    )
    assert resp.status_code == 422
    assert "nodeSelector" in resp.text or "not an address" in resp.text


@pytest.mark.asyncio
async def test_env_and_volumes_live_in_the_recipe(client):
    resp = await client.post(
        "/api/campaigns",
        json=_body(
            recipe_text=QWEN.read_text(),
            extra_env={"A": "1"},
        ),
    )
    assert resp.status_code == 422 and "values.env" in resp.text


@pytest.mark.asyncio
async def test_a_campaign_without_a_recipe_still_needs_an_image(client):
    resp = await client.post("/api/campaigns", json=_body(engine="sglang"))
    assert resp.status_code == 422 and "image" in resp.text


@pytest.mark.asyncio
async def test_reading_a_recipe_says_what_it_would_launch(client):
    resp = await client.post("/api/campaigns/recipe", json={"text": QWEN.read_text()})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["source"] == "variant" and out["engine"] == "sglang"
    assert out["problems"] == [] and out["shape"]["total_gpus"] == 2
    assert out["commands"]["engine"][:2] == ["sglang", "serve"]
    assert "extraArgs" in out["yaml"]
    bad = await client.post("/api/campaigns/recipe", json={"text": "hello: world"})
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_a_range_is_pinned_later_and_an_exact_version_now(client):
    ranged = await client.post("/api/campaigns", json=_body(recipe_text=QWEN.read_text()))
    assert ranged.status_code == 200, ranged.text
    assert ranged.json()["chart_version"] == ""
    exact = QWEN.read_text().replace("version: '>=0.7.1'", "version: 0.9.26")
    pinned = await client.post("/api/campaigns", json=_body(recipe_text=exact))
    assert pinned.json()["chart_version"] == "0.9.26"
    chosen = await client.post(
        "/api/campaigns", json=_body(recipe_text=QWEN.read_text(), chart_version="0.9.20")
    )
    assert chosen.json()["chart_version"] == "0.9.20"


def test_the_first_run_pins_what_the_operator_installed():
    from app.control.orchestrator.supervisor import Supervisor

    recipe = parse(QWEN.read_text()).recipe
    campaign = SimpleNamespace(id=1, recipe=recipe.model_dump(), chart_version="")
    env = {"chart_version": "0.9.26"}
    driver = SimpleNamespace(environment=lambda handle: env)
    fake = SimpleNamespace(_driver_for=lambda machine: driver)

    def run():
        return SimpleNamespace(id=1, machine=None, gpu_indices=[0], campaign=campaign)

    Supervisor._snapshot_environment(fake, run(), SimpleNamespace(machine=None))
    assert campaign.chart_version == "0.9.26"
    # Pinned once: a later run that met a newer chart does not move it.
    env["chart_version"] = "0.9.27"
    Supervisor._snapshot_environment(fake, run(), SimpleNamespace(machine=None))
    assert campaign.chart_version == "0.9.26"
