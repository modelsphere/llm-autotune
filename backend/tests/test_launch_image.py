"""(1.9) A policy's launch may name another of the campaign's images.

The campaign lists them in `policy_settings.images`; a launch naming one runs
it, with the same engine and configuration, and records it on the run.
"""
# ruff: noqa: F811  (the `night` fixture is imported, then named as a parameter)

from sqlalchemy import select

from app.db.models import Campaign, Event, Run
from tests.test_external_policy import _make_external, _open
from tests.test_policy_sessions import night  # noqa: F401  (fixture)


async def _allow(http, images):
    async with http.db() as session:
        campaign = await session.get(Campaign, 1)
        campaign.policy_settings = {**(campaign.policy_settings or {}), "images": images}
        await session.commit()


async def test_a_launch_runs_the_image_it_names(night):
    supervisor, driver, http = night
    await _make_external(http)
    await _allow(http, ["sglang:next"])
    policy = await _open(supervisor, http)
    specs = []
    launch = driver.launch
    driver.launch = lambda spec: (specs.append(spec), launch(spec))[1]

    args = {"tp_size": 2}
    other = await policy.post("/launches", json={"engine_args": args, "image": "sglang:next"})
    own = await policy.post("/launches", json={"engine_args": args, "image": "sglang:test"})
    assert other.status_code == own.status_code == 201, own.text
    assert other.json()["image"] == "sglang:next"
    assert own.json()["image"] == "", "naming the campaign's own image stores \"\""
    for _ in range(3):
        supervisor.tick()
    by_run = {s.run_id: s.image for s in specs}
    assert by_run[other.json()["id"]] == "sglang:next"
    assert by_run[own.json()["id"]] == "sglang:test"
    async with http.db() as session:
        run = await session.get(Run, other.json()["id"])
        assert run.image == "sglang:next"
        asked = select(Event).where(Event.kind == "policy_launch_requested")
        audit = (await session.execute(asked)).scalars().all()
        assert {e.payload["image"] for e in audit} == {"sglang:next", "sglang:test"}


async def test_an_image_the_campaign_does_not_allow_is_refused(night):
    supervisor, _driver, http = night
    await _make_external(http)
    await _allow(http, ["sglang:next"])
    policy = await _open(supervisor, http)

    body = {"engine_args": {"tp_size": 2}, "image": "sglang:other"}
    refused = await policy.post("/launches", json=body)
    assert refused.status_code == 422
    assert "sglang:next" in refused.text and "sglang:test" in refused.text
    checked = await policy.post("/configs/check", json=body)
    assert checked.status_code == 422


async def test_the_vocabulary_answers_for_another_allowed_image(night):
    supervisor, _driver, http = night
    await _make_external(http)
    await _allow(http, ["sglang:next"])
    policy = await _open(supervisor, http)

    answered = await policy.get("/engine/vocabulary?image=sglang:next")
    assert answered.status_code == 200, answered.text
    assert answered.json()["image"] == "sglang:next"
    assert (await policy.get("/engine/vocabulary?image=nope:1")).status_code == 422


async def test_the_launched_recipe_runs_the_image_it_names(night):
    """(1.10) The recipe a launch carries — what the LLMService driver renders
    — names the launch's image, not the campaign's, and the launch answers it
    with the config's identity."""
    supervisor, driver, http = night
    await _make_external(http)
    await _allow(http, ["registry:5000/sglang:next"])
    policy = await _open(supervisor, http)
    specs = []
    launch = driver.launch
    driver.launch = lambda spec: (specs.append(spec), launch(spec))[1]

    args = {"tp_size": 2}
    other = await policy.post(
        "/launches", json={"engine_args": args, "image": "registry:5000/sglang:next"}
    )
    own = await policy.post("/launches", json={"engine_args": args})
    assert other.status_code == own.status_code == 201, other.text
    for _ in range(3):
        supervisor.tick()
    recipes = {s.run_id: s.recipe for s in specs}
    image = recipes[other.json()["id"]]["values"]["image"]
    assert image == {"repository": "registry:5000/sglang", "tag": "next"}, "the port is not a tag"
    assert recipes[own.json()["id"]]["values"]["image"] == {"repository": "sglang", "tag": "test"}

    answered = (await policy.get(f"/launches/{other.json()['id']}")).json()
    assert answered["recipe"]["values"]["image"] == image
    assert other.json()["recipe"] == answered["recipe"]
    assert answered["config_hash"].startswith("sha256:")
    assert answered["config_hash"] != own.json()["config_hash"], "another image, another config"

    checked = await policy.post(
        "/configs/check", json={"engine_args": args, "image": "registry:5000/sglang:next"}
    )
    assert checked.json()["recipe"]["values"]["image"] == image
