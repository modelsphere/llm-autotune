"""What plugins keep about a campaign travels with it.

A campaign is created with `extensions: {<plugin name>: data}`; each plugin
gets its part in the same transaction and may refuse it. The campaign reads
back with what the plugins keep, and its spec and its clones carry that on.
A key no enabled plugin takes is refused, not dropped; a plugin that fails
to read is left out, never a campaign that cannot be read.
"""

from itertools import count

import httpx
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import plugins
from app.core.auth import create_token
from app.db.base import Base, get_async_session
from app.db.models import Campaign, User
from app.main import app
from app.plugins import PLUGIN_API_VERSION, ExtensionRefused, Plugin

_counter = count()
KEPT: dict[int, dict] = {}


def _created(session, campaign, data):
    if data.get("refuse"):
        raise ExtensionRefused("tags: refused on purpose")
    KEPT[campaign.id] = dict(data)


def _read(session, campaigns):
    return {c.id: KEPT[c.id] for c in campaigns if c.id in KEPT}


def _broken_read(session, campaigns):
    raise RuntimeError("cannot read")


TAGS = Plugin(
    name="tags",
    api_version=PLUGIN_API_VERSION,
    on_campaign_created=_created,
    campaign_extensions=_read,
)
BROKEN = Plugin(name="broken", api_version=PLUGIN_API_VERSION, campaign_extensions=_broken_read)


@pytest_asyncio.fixture
async def client(monkeypatch):
    KEPT.clear()
    monkeypatch.setattr(plugins, "enabled", lambda: (TAGS, BROKEN))
    name = f"campaign_ext_{next(_counter)}"
    engine = create_async_engine(f"sqlite+aiosqlite:///file:{name}?mode=memory&cache=shared&uri=true")
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
    return {
        "name": "c", "engine": "sglang", "image": "img", "model_path": "/m",
        "served_model_name": "m", "search_space": {"grid": {"tp": [1]}}, **extra,
    }


async def _campaign_count(client) -> int:
    async with client.db() as session:
        return len((await session.execute(select(Campaign))).scalars().all())


async def test_each_plugin_gets_its_part_and_it_reads_back(client, caplog):
    created = await client.post("/api/campaigns", json=_body(extensions={"tags": {"team": "a"}}))
    assert created.status_code == 200, created.text
    campaign_id = created.json()["id"]
    assert KEPT[campaign_id] == {"team": "a"}
    assert created.json()["extensions"] == {"tags": {"team": "a"}}

    assert (await client.get(f"/api/campaigns/{campaign_id}")).json()["extensions"] == {
        "tags": {"team": "a"}
    }
    listed = (await client.get("/api/campaigns")).json()
    assert listed[0]["extensions"] == {"tags": {"team": "a"}}
    # The plugin that cannot read is left out, and says so in the log.
    assert "plugin broken: reading campaign extensions failed" in caplog.text


async def test_spec_and_clone_carry_them(client):
    campaign_id = (
        await client.post("/api/campaigns", json=_body(extensions={"tags": {"team": "a"}}))
    ).json()["id"]

    spec = (await client.get(f"/api/campaigns/{campaign_id}/spec")).json()
    assert spec["extensions"] == {"tags": {"team": "a"}}

    clone = await client.post(f"/api/campaigns/{campaign_id}/clone", json={"name": "copy"})
    assert clone.status_code == 200, clone.text
    assert KEPT[clone.json()["id"]] == {"team": "a"}

    changed = await client.post(
        f"/api/campaigns/{campaign_id}/clone",
        json={"name": "other team", "overrides": {"extensions": {"tags": {"team": "b"}}}},
    )
    assert KEPT[changed.json()["id"]] == {"team": "b"}


async def test_a_refused_part_refuses_the_campaign(client):
    refused = await client.post("/api/campaigns", json=_body(extensions={"tags": {"refuse": 1}}))

    assert refused.status_code == 422
    assert "refused on purpose" in refused.text
    assert await _campaign_count(client) == 0


async def test_a_key_no_enabled_plugin_takes_is_refused_not_dropped(client):
    refused = await client.post("/api/campaigns", json=_body(extensions={"nobody": {"x": 1}}))

    assert refused.status_code == 422
    assert "nobody" in refused.text
    assert await _campaign_count(client) == 0


async def test_without_extensions_nothing_changes(client):
    created = await client.post("/api/campaigns", json=_body())

    assert created.status_code == 200, created.text
    assert created.json()["extensions"] == {}
    assert KEPT == {}
