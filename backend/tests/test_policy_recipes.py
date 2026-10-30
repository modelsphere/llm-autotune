"""Contract 1.6: configs address a recipe.

A policy sees the recipe every launch starts from in its manifest. A config's
keys may name a pd role's flags (`prefill.`/`decode.`) or any chart value
(`values.<path>`) as well as flags and env. A policy sends configs only; the
check answers with the recipe the platform builds from one."""

from app.control.launch_config import LaunchConfig
from app.control.recipe.legacy import from_fields
from app.db.models import Baseline, Campaign, Run
from tests.test_policy_sessions import _boot, night  # noqa: F401  (night is a fixture)


async def test_the_manifest_carries_the_recipe(night):  # noqa: F811
    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)
    manifest = (await policy.get("/session")).json()
    assert manifest["contract"]["version"] == "1.7"
    assert manifest["services"]["recipes"] is True
    recipe = manifest["model"]["recipe"]
    assert recipe["chart"]["name"] == "sglang"
    # The space's base is in it: the campaign serves with the cache report on.
    assert "--enable-cache-report" in recipe["values"]["extraArgs"]


async def test_chart_values_are_addressable_and_site_values_are_not(night, monkeypatch):  # noqa: F811
    from app.api import policy_sessions

    monkeypatch.setattr(policy_sessions, "_launches_recipes", lambda machine: True)
    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)
    ok = (
        await policy.post(
            "/configs/check",
            json={
                "engine_args": {
                    "tp_size": 2,
                    "values.shm.sizeLimit": "32Gi",
                }
            },
        )
    ).json()
    assert ok["ok"] is True, ok["problems"]
    assert ok["recipe"]["values"]["shm"] == {"sizeLimit": "32Gi"}
    assert ok["shape"]["total_gpus"] == 2
    argv = ok["commands"]["engine"]
    assert argv[:2] == ["sglang", "serve"] and "--tp-size=2" in argv
    bad = (
        await policy.post(
            "/configs/check",
            json={
                "engine_args": {
                    "values.nodeSelector.zone": "a",
                    "pd.prefill.replicas": 2,
                    "values.extraArgs": [],
                }
            },
        )
    ).json()
    assert bad["ok"] is False
    assert {p["param"]: p["kind"] for p in bad["problems"]} == {
        "values.nodeSelector.zone": "placement",
        "pd.prefill.replicas": "bad_key",
        "values.extraArgs": "bad_key",
    }


async def test_the_check_answers_with_the_recipe_a_config_builds(night):  # noqa: F811
    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)
    checked = (
        await policy.post("/configs/check", json={"engine_args": {"mem_fraction_static": 0.8}})
    ).json()
    assert checked["ok"] is True, checked["problems"]
    assert "--mem-fraction-static=0.8" in checked["recipe"]["values"]["extraArgs"]
    # A whole recipe is not a config: the platform builds it, a policy does not.
    recipe = (await policy.get("/session")).json()["model"]["recipe"]
    refused = await policy.post("/configs/check", json={"recipe": recipe})
    assert refused.status_code == 422


async def test_a_recipe_campaign_refuses_what_its_chart_would(night):  # noqa: F811
    supervisor, driver, http = night
    async with http.db() as session:
        campaign = await session.get(Campaign, 1)
        campaign.recipe = from_fields(
            engine="sglang",
            image="lmsysorg/sglang:v0.5.20",
            served_model_name="m",
            engine_args={"tp": 1},
        ).model_dump()
        await session.commit()
    policy = await _boot(supervisor, driver, http)
    checked = (
        await policy.post("/configs/check", json={"engine_args": {"node_rank": 1}})
    ).json()
    assert checked["ok"] is False
    assert any("rendered by the chart" in p["message"] for p in checked["problems"])


async def test_a_driver_that_takes_flags_only_refuses_chart_values(night):  # noqa: F811
    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)
    checked = (
        await policy.post(
            "/configs/check",
            json={
                "engine_args": {
                    "values.shm.sizeLimit": "32Gi",
                }
            },
        )
    ).json()
    assert [p["kind"] for p in checked["problems"]] == ["substrate"]


async def test_a_recipe_launch_takes_the_cards_its_shape_needs(night):  # noqa: F811
    """A policy counts cards from the config's own tp×dp×pp; under a recipe
    the parallelism can live in the recipe (here tp 4, or lws.size), so a
    config that does not touch it asked for one card and was refused, and the
    campaign ended with nothing run. The recipe's shape decides now."""
    supervisor, driver, http = night
    async with http.db() as session:
        campaign = await session.get(Campaign, 1)
        campaign.recipe = from_fields(
            engine="sglang",
            image="lmsysorg/sglang:v0.5.20",
            served_model_name="m",
            engine_args={"tp": 4},
        ).model_dump()
        await session.commit()
    policy = await _boot(supervisor, driver, http)
    launched = await policy.post(
        "/launches", json={"engine_args": {"mem_fraction_static": 0.8}, "gpu_indices": [0]}
    )
    assert launched.status_code == 201, launched.text
    async with http.db() as session:
        run = await session.get(Run, launched.json()["id"])
        assert run.gpu_indices == [0, 1, 2, 3]
    # The next one gets the cards left, and then waits.
    second = await policy.post(
        "/launches", json={"engine_args": {"mem_fraction_static": 0.7}, "gpu_indices": [0]}
    )
    assert second.status_code == 201, second.text
    third = await policy.post(
        "/launches", json={"engine_args": {"mem_fraction_static": 0.6}, "gpu_indices": [0]}
    )
    assert third.status_code == 429, third.text


def _glm_recipe():
    return from_fields(
        engine="sglang",
        image="lmsysorg/sglang:v0.5.22",
        served_model_name="m",
        engine_args={"tp": 1, "mem_fraction_static": 0.83, "reasoning_parser": "glm45",
                     "chunked_prefill_size": 8192},
        extra_env={"PYTORCH_ALLOC_CONF": "expandable_segments:True"},
    ).model_dump()


async def _recipe_campaign(http, **baseline):
    async with http.db() as session:
        campaign = await session.get(Campaign, 1)
        campaign.recipe = _glm_recipe()
        campaign.search_space = {"base": {}, "grid": {"mem_fraction_static": [0.8, 0.9]}}
        if baseline:
            session.add(Baseline(served_model_name=campaign.served_model_name, engine="sglang",
                                 card_type="", **baseline))
        await session.commit()


async def test_production_is_the_recipe_when_none_is_recorded(night):  # noqa: F811
    """1.7: with nothing on Baselines, production is the recipe as written —
    built by the platform and handed over whole, not left for the policy to
    dig out of the recipe."""
    supervisor, driver, http = night
    await _recipe_campaign(http)
    policy = await _boot(supervisor, driver, http)
    manifest = (await policy.get("/session")).json()
    assert manifest["services"]["production_config"] is True
    production = manifest["production"]
    assert production["source"] == "recipe"
    assert production["config"] == {}
    assert production["image"] == "lmsysorg/sglang:v0.5.22"
    assert production["engine_args"]["mem_fraction_static"] == 0.83
    assert production["engine_args"]["reasoning_parser"] == "glm45"
    assert production["env"]["PYTORCH_ALLOC_CONF"] == "expandable_segments:True"
    # Its config launches exactly that recipe.
    checked = (await policy.post("/configs/check", json={"engine_args": production["config"]})).json()
    assert checked["ok"] is True, checked["problems"]
    assert checked["recipe"]["values"]["extraArgs"] == manifest["model"]["recipe"]["values"]["extraArgs"]


async def test_a_recorded_baseline_is_applied_over_the_recipe(night):  # noqa: F811
    supervisor, driver, http = night
    await _recipe_campaign(http, engine_args={"mem_fraction_static": 0.9})
    policy = await _boot(supervisor, driver, http)
    production = (await policy.get("/session")).json()["production"]
    assert production["source"] == "baselines"
    assert production["config"] == {"mem_fraction_static": 0.9}
    # The recorded knob over the recipe's: the rest is still the recipe's.
    assert production["engine_args"]["mem_fraction_static"] == 0.9
    assert production["engine_args"]["chunked_prefill_size"] == 8192


async def test_a_forbidden_flag_at_the_recipes_own_value_is_unchanged(night):  # noqa: F811
    supervisor, driver, http = night
    await _recipe_campaign(http)
    policy = await _boot(supervisor, driver, http)
    same = (await policy.post("/configs/check",
                              json={"engine_args": {"reasoning_parser": "glm45"}})).json()
    assert not [p for p in same["problems"] if p["kind"] == "forbidden"], same["problems"]
    other = (await policy.post("/configs/check",
                               json={"engine_args": {"reasoning_parser": "qwen3"}})).json()
    assert [p["param"] for p in other["problems"] if p["kind"] == "forbidden"] == ["reasoning_parser"]


def test_a_recipe_campaigns_launch_config_is_its_recipe():
    """The one starting-config shape reads a recipe campaign through its
    recipe: a candidate's knobs over the recipe's flags and env, not the knobs
    alone."""

    class _Campaign:
        engine, image, model_path, served_model_name, service_port = (
            "sglang", "", "/models/m", "m", 0)
        extra_env, extra_volumes = {}, {}
        recipe = _glm_recipe()
        search_space = {"base": {}}

    cfg = LaunchConfig.from_campaign(_Campaign(), {"mem_fraction_static": 0.9}, gpu_type="H100")
    assert cfg.image == "lmsysorg/sglang:v0.5.22"
    assert cfg.engine_args["mem_fraction_static"] == 0.9
    assert cfg.engine_args["chunked_prefill_size"] == 8192
    assert cfg.extra_env["PYTORCH_ALLOC_CONF"] == "expandable_segments:True"
    assert (cfg.model_path, cfg.gpu_type) == ("/models/m", "H100")
