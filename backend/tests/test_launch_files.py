"""(1.9) A policy's launch may mount files into its engine container.

Only where the campaign allows it (`policy_settings.files`, optionally under
`file_paths`), on a driver that can mount them. The run stores the files, the
launch answers them by digest, and the driver gets them with the spec.
"""
# ruff: noqa: F811  (the `night` fixture is imported, then named as a parameter)

import hashlib

from sqlalchemy import select

from app.api import policy_sessions
from app.db.models import Campaign, Event, Run
from tests.test_external_policy import _make_external, _open
from tests.test_policy_sessions import night  # noqa: F401  (fixture)

MOE = "/opt/configs/moe/E=128,N=768.json"


async def _allow(http, **settings):
    async with http.db() as session:
        campaign = await session.get(Campaign, 1)
        campaign.policy_settings = {**(campaign.policy_settings or {}), **settings}
        await session.commit()


def _mounting(driver, monkeypatch, mounts=True):
    driver.mounts_files = mounts
    monkeypatch.setattr(policy_sessions, "_engine_driver", lambda machine: driver)


async def test_a_launchs_files_reach_the_driver_and_are_answered_by_digest(night, monkeypatch):
    supervisor, driver, http = night
    _mounting(driver, monkeypatch)
    await _make_external(http)
    await _allow(http, files=True, file_paths=["/opt/configs"])
    policy = await _open(supervisor, http)
    specs = []
    launch = driver.launch
    driver.launch = lambda spec: (specs.append(spec), launch(spec))[1]

    content = '{"1": {"BLOCK_SIZE_M": 16}}'
    made = await policy.post(
        "/launches", json={"engine_args": {"tp_size": 2}, "files": {MOE: content}}
    )
    assert made.status_code == 201, made.text
    digest = hashlib.sha256(content.encode()).hexdigest()
    assert made.json()["files"] == [{"path": MOE, "sha256": digest, "bytes": len(content)}]
    assert made.json()["tainted"] is False
    for _ in range(3):
        supervisor.tick()
    assert [s.files for s in specs] == [{MOE: content}]
    async with http.db() as session:
        assert (await session.get(Run, made.json()["id"])).files == {MOE: content}
        asked = select(Event).where(Event.kind == "policy_launch_requested")
        (audit,) = (await session.execute(asked)).scalars().all()
        assert audit.payload["files"][0]["sha256"] == digest
    assert (await policy.get(f"/launches/{made.json()['id']}")).json()["files"][0]["path"] == MOE


async def test_files_are_refused_unless_the_campaign_allows_them(night, monkeypatch):
    supervisor, driver, http = night
    _mounting(driver, monkeypatch)
    await _make_external(http)
    policy = await _open(supervisor, http)
    body = {"engine_args": {"tp_size": 2}, "files": {MOE: "{}"}}

    refused = await policy.post("/launches", json=body)
    assert refused.status_code == 422 and "policy_settings.files" in refused.text

    await _allow(http, files=True, file_paths=["/etc/templates"])
    outside = await policy.post("/launches", json=body)
    assert outside.status_code == 422 and "/etc/templates" in outside.text

    await _allow(http, files=True, file_paths=[])
    assert (await policy.post("/launches", json=body)).status_code == 201
    async with http.db() as session:
        assert len((await session.execute(select(Run))).scalars().all()) == 1


async def test_a_driver_that_cannot_mount_files_refuses_them(night, monkeypatch):
    supervisor, driver, http = night
    _mounting(driver, monkeypatch, mounts=False)
    await _make_external(http)
    await _allow(http, files=True)
    policy = await _open(supervisor, http)
    refused = await policy.post(
        "/launches", json={"engine_args": {"tp_size": 2}, "files": {MOE: "{}"}}
    )
    assert refused.status_code == 501
    # No files, nothing to mount: launched as always.
    assert (await policy.post("/launches", json={"engine_args": {"tp_size": 2}})).status_code == 201


async def test_a_files_path_must_be_absolute_and_name_a_file(night, monkeypatch):
    supervisor, driver, http = night
    _mounting(driver, monkeypatch)
    await _make_external(http)
    await _allow(http, files=True)
    policy = await _open(supervisor, http)
    for path in ("relative.json", "/opt/../etc/passwd", "/opt/dir/", "/opt//x"):
        bad = await policy.post(
            "/launches", json={"engine_args": {"tp_size": 2}, "files": {path: "x"}}
        )
        assert bad.status_code == 422, path
    big = {f"/opt/f{i}": "x" * 40_000 for i in range(14)}
    assert (await policy.post(
        "/launches", json={"engine_args": {"tp_size": 2}, "files": big}
    )).status_code == 422


async def test_the_campaigns_files_mount_on_every_launch_under_the_launchs_own(night, monkeypatch):
    """A campaign's files are its base config's: every launch mounts them, and
    a launch's own file at the same path wins. Files change the config's
    identity (test_recipe_campaigns: without any, the recipe's own hash)."""
    supervisor, driver, http = night
    _mounting(driver, monkeypatch)
    await _make_external(http)
    await _allow(http, files=True)
    template = "/opt/templates/chat.jinja"
    async with http.db() as session:
        campaign = await session.get(Campaign, 1)
        campaign.files = {MOE: "base", template: "{{ messages }}"}
        await session.commit()
    policy = await _open(supervisor, http)

    plain = await policy.post("/launches", json={"engine_args": {"tp_size": 2}})
    tuned = await policy.post(
        "/launches", json={"engine_args": {"tp_size": 2}, "files": {MOE: "tuned"}}
    )
    assert plain.status_code == tuned.status_code == 201, tuned.text
    async with http.db() as session:
        assert (await session.get(Run, plain.json()["id"])).files == {
            MOE: "base", template: "{{ messages }}"
        }
        assert (await session.get(Run, tuned.json()["id"])).files == {
            MOE: "tuned", template: "{{ messages }}"
        }
    assert plain.json()["recipe"] == tuned.json()["recipe"]
    assert plain.json()["config_hash"] != tuned.json()["config_hash"]

