"""The plugin API, held to its word through the example plugin.

These run only where the example plugin is installed — CI's plugin job does
`uv pip install -e tests/plugins/example` and sets AUTOTUNE_PLUGINS=example —
so a change to the platform that would break a plugin fails there, in the
pull request that makes it.
"""

from itertools import count
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from app import plugins
from app.control.orchestrator.supervisor import Supervisor
from app.core.auth import create_token
from app.db.base import Base, get_async_session
from app.db.models import Campaign, User
from tests.fakes import NullDriver, StubEvaluator

example_plugin = pytest.importorskip("example_plugin")
from example_plugin.models import Base as PluginBase  # noqa: E402
from example_plugin.models import CampaignLabel, Setting, TickNote  # noqa: E402

_counter = count()


def test_it_is_found_through_its_entry_point():
    (loaded,) = plugins.load(["example"])
    assert loaded is example_plugin.plugin
    assert loaded.api_version == plugins.PLUGIN_API_VERSION


def _database(tmp_path: Path) -> tuple[str, object]:
    url = f"sqlite:///{tmp_path / 'autotune.sqlite'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    return url, engine


def test_its_migrations_build_its_schema_beside_the_platforms(tmp_path):
    url, engine = _database(tmp_path)

    plugins.upgrade(example_plugin.plugin, url)
    plugins.upgrade(example_plugin.plugin, url)  # every deploy runs it again

    tables = set(inspect(engine).get_table_names())
    assert set(PluginBase.metadata.tables) <= tables
    assert "campaigns" in tables
    with engine.connect() as connection:
        version = connection.execute(text("SELECT version_num FROM alembic_version_example"))
        assert version.scalar() == "example_001"
        # `alembic check` for the plugin: its migrations and its models agree,
        # and the platform's tables are none of its business.
        context = MigrationContext.configure(
            connection, opts={"include_object": plugins.only_tables_of(PluginBase.metadata)}
        )
        assert compare_metadata(context, PluginBase.metadata) == []


def test_its_bootstrap_only_adds_what_is_missing(tmp_path):
    url, engine = _database(tmp_path)
    plugins.upgrade(example_plugin.plugin, url)

    example_plugin.plugin.on_bootstrap(engine)
    with sessionmaker(engine)() as session:
        session.get(Setting, "greeting").value = "changed by someone"
        session.commit()
    example_plugin.plugin.on_bootstrap(engine)

    with sessionmaker(engine)() as session:
        assert session.get(Setting, "greeting").value == "changed by someone"


def test_its_tick_step_runs_in_the_workers_tick(tmp_path, monkeypatch):
    url, engine = _database(tmp_path)
    plugins.upgrade(example_plugin.plugin, url)
    factory = sessionmaker(engine, expire_on_commit=False)
    supervisor = Supervisor(
        session_factory=factory,
        health_evaluator=StubEvaluator(),
        bench_evaluator=StubEvaluator(),
    )
    supervisor.driver = NullDriver()
    monkeypatch.setattr(plugins, "enabled", lambda: (example_plugin.plugin,))

    supervisor.tick()
    supervisor.tick()

    with factory() as session:
        notes = session.scalars(select(TickNote)).all()
    assert [n.active_campaigns for n in notes] == [0, 0]


@pytest_asyncio.fixture
async def client():
    """The platform's app, with the example plugin's routes mounted at import
    (AUTOTUNE_PLUGINS=example), on one in-memory database holding both the
    platform's tables and the plugin's."""
    if "example" not in plugins.configured_names():
        pytest.skip("AUTOTUNE_PLUGINS does not enable the example plugin")
    from app.main import app

    name = f"plugin_api_{next(_counter)}"
    engine = create_async_engine(f"sqlite+aiosqlite:///file:{name}?mode=memory&cache=shared&uri=true")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(PluginBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _session():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_async_session] = _session
    async with factory() as session:
        user = User(id=1, username="admin", password_hash="x", role="admin")
        session.add(user)
        session.add(
            Campaign(
                id=7, owner_id=1, name="c", engine="sglang", image="img",
                model_path="/models/m", served_model_name="m", search_space={},
            )
        )
        session.add(Setting(key="greeting", value="hello"))
        await session.commit()
        token = create_token(user)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
        http.headers["Authorization"] = f"Bearer {token}"
        http.db = factory
        yield http
    app.dependency_overrides.clear()
    await engine.dispose()


async def test_its_routes_are_mounted_under_api_and_behind_login(client):
    assert (await client.get("/api/example/settings/greeting")).json() == {
        "key": "greeting",
        "value": "hello",
    }
    assert (await client.get("/api/example/ticks")).json() == {
        "ticks": 0,
        "active_campaigns": None,
    }
    put = await client.put("/api/example/campaigns/7/label", json={"label": "nightly"})
    assert put.status_code == 200
    async with client.db() as session:
        assert (await session.get(CampaignLabel, 7)).label == "nightly"

    anonymous = await client.get(
        "/api/example/ticks", headers={"Authorization": ""}
    )
    assert anonymous.status_code == 401


async def test_the_platforms_routes_are_untouched(client):
    assert (await client.get("/api/health")).json() == {"status": "ok"}


def test_its_planner_plans_only_the_campaigns_it_labels(tmp_path, monkeypatch):
    from app.db.models import BaselineStatus, CampaignStatus, Candidate, Machine, MachineState

    url, engine = _database(tmp_path)
    plugins.upgrade(example_plugin.plugin, url)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add(User(id=1, username="u", password_hash="x"))
        session.add(
            Machine(
                id=1, name="gpu-01", host="10.0.0.1", gpu_count=8,
                state=MachineState.AVAILABLE.value,
                baseline_status=BaselineStatus.CLEARED.value,
            )
        )
        for campaign_id in (1, 2):
            session.add(
                Campaign(
                    id=campaign_id, owner_id=1, name=f"c{campaign_id}", engine="sglang",
                    image="img", model_path="/m", served_model_name="m",
                    search_space={"grid": {"tp": [1, 2, 4]}},
                    status=CampaignStatus.ACTIVE.value, run_baseline_canary=False,
                )
            )
        session.flush()
        session.add(CampaignLabel(campaign_id=1, label="plan:reverse"))
        session.commit()
    supervisor = Supervisor(session_factory=factory)
    supervisor.driver = NullDriver()
    monkeypatch.setattr(plugins, "enabled", lambda: (example_plugin.plugin,))

    with factory() as session:
        supervisor._plan(session)
        session.commit()
        planned = {
            campaign_id: [
                c.config["tp"]
                for c in session.scalars(
                    select(Candidate)
                    .where(Candidate.campaign_id == campaign_id)
                    .order_by(Candidate.id)
                )
            ]
            for campaign_id in (1, 2)
        }

    assert planned == {1: [4, 2, 1], 2: [1, 2, 4]}, "labelled: reversed; the other: enumerated"


async def test_a_campaign_is_created_and_read_with_its_label(client):
    body = {
        "name": "labelled", "engine": "sglang", "image": "img", "model_path": "/m",
        "served_model_name": "m", "search_space": {"grid": {"tp": [1]}},
        "extensions": {"example": {"label": "plan:reverse"}},
    }
    created = await client.post("/api/campaigns", json=body)
    assert created.status_code == 200, created.text
    campaign_id = created.json()["id"]
    assert created.json()["extensions"] == {"example": {"label": "plan:reverse"}}
    async with client.db() as session:
        assert (await session.get(CampaignLabel, campaign_id)).label == "plan:reverse"

    spec = (await client.get(f"/api/campaigns/{campaign_id}/spec")).json()
    assert spec["extensions"] == {"example": {"label": "plan:reverse"}}

    refused = await client.post(
        "/api/campaigns", json={**body, "extensions": {"example": {"label": ""}}}
    )
    assert refused.status_code == 422
    assert "label must be 1 to 64 characters" in refused.text
