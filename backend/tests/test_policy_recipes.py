"""Contract 1.6: configs address a recipe, and a recipe can be sent whole.

A policy sees the recipe every launch starts from in its manifest. A config's
keys may name a pd role's flags (`prefill.`/`decode.`) or any chart value
(`values.<path>`) as well as flags and env, and a launch, check or contender
may carry a whole recipe instead, read as the addresses where it differs."""

import copy

from app.control.recipe.legacy import from_fields
from app.db.models import Campaign
from tests.test_policy_sessions import _boot, night  # noqa: F401  (night is a fixture)


async def test_the_manifest_carries_the_recipe(night):  # noqa: F811
    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)
    manifest = (await policy.get("/session")).json()
    assert manifest["contract"]["version"] == "1.6"
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


async def test_a_whole_recipe_reads_as_the_addresses_it_changes(night):  # noqa: F811
    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)
    recipe = copy.deepcopy((await policy.get("/session")).json()["model"]["recipe"])
    recipe["values"]["extraArgs"].append("--mem-fraction-static=0.8")
    checked = (await policy.post("/configs/check", json={"recipe": recipe})).json()
    assert checked["ok"] is True, checked["problems"]
    assert checked["config"]["mem_fraction_static"] == 0.8
    launched = await policy.post("/launches", json={"recipe": recipe, "gpu_indices": [0]})
    assert launched.status_code == 201, launched.text
    assert launched.json()["config"]["mem_fraction_static"] == 0.8


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
    recipe = copy.deepcopy((await policy.get("/session")).json()["model"]["recipe"])
    recipe["values"]["extraArgs"].append("--port=9000")
    checked = (await policy.post("/configs/check", json={"recipe": recipe})).json()
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
