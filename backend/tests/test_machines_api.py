"""Editing and removing a resource, and the guard that keeps a machine a
campaign still depends on from being changed out from under it."""

from itertools import count

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.auth import create_token
from app.db.base import Base, get_async_session
from app.db.models import (
    Campaign,
    CampaignStatus,
    Candidate,
    CandidateStatus,
    Machine,
    MachineState,
    Run,
    RunStatus,
    User,
)
from app.main import app

_counter = count()


@pytest_asyncio.fixture
async def client():
    name = f"mc_{next(_counter)}"
    db = f"sqlite+aiosqlite:///file:{name}?mode=memory&cache=shared&uri=true"
    engine = create_async_engine(db)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _session():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_async_session] = _session
    async with factory() as session:
        session.add(User(id=1, username="admin", password_hash="x", role="admin"))
        await session.commit()
        token = create_token(await session.get(User, 1))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
        http.headers["Authorization"] = f"Bearer {token}"
        http.factory = factory
        yield http

    app.dependency_overrides.clear()
    await engine.dispose()


async def _machine(client, name="node-1", **over) -> dict:
    body = {"name": name, "host": "10.0.0.1", "gpu_count": 8, **over}
    return (await client.post("/api/machines", json=body)).json()


async def _pin_campaign(factory, machine_name: str, status: str) -> None:
    async with factory() as session:
        session.add(Campaign(
            owner_id=1, name=f"camp-{status}", engine="sglang", image="img",
            model_path="/m", served_model_name="glm-5", search_space={},
            objective={}, status=status, machine_names=[machine_name],
        ))
        await session.commit()


# -- edit ---------------------------------------------------------------------


async def test_edit_updates_fields_and_lists_back(client):
    m = await _machine(client, "node-1", gpu_type="A100", notes="old")
    body = {"name": "node-1", "host": "10.0.0.9", "gpu_count": 4,
            "gpu_type": "H100", "notes": "new"}
    edited = (await client.put(f"/api/machines/{m['id']}", json=body)).json()
    assert edited["gpu_count"] == 4
    assert edited["gpu_type"] == "H100"
    assert edited["host"] == "10.0.0.9"
    assert edited["notes"] == "new"


async def test_edit_rejects_a_name_already_taken(client):
    await _machine(client, "node-1")
    other = await _machine(client, "node-2")
    resp = await client.put(f"/api/machines/{other['id']}",
                            json={"name": "node-1", "host": "h", "gpu_count": 8})
    assert resp.status_code == 409
    assert "taken" in resp.json()["detail"]


# -- delete -------------------------------------------------------------------


async def test_delete_removes_the_machine(client):
    m = await _machine(client, "node-1")
    assert (await client.delete(f"/api/machines/{m['id']}")).status_code == 200
    assert (await client.get("/api/machines")).json() == []


async def test_delete_detaches_finished_runs_rather_than_erasing_them(client):
    m = await _machine(client, "node-1")
    async with client.factory() as session:
        session.add(Campaign(id=1, owner_id=1, name="done", engine="sglang", image="i",
                             model_path="/m", served_model_name="glm-5", search_space={},
                             objective={}, status=CampaignStatus.DONE.value,
                             machine_names=["node-1"]))
        session.add(Candidate(id=1, campaign_id=1, config={"tp": 2}, config_hash="h1",
                              status=CandidateStatus.EXHAUSTED.value))
        session.add(Run(id=1, campaign_id=1, candidate_id=1, machine_id=m["id"],
                        status=RunStatus.SUCCEEDED.value))
        await session.commit()

    assert (await client.delete(f"/api/machines/{m['id']}")).status_code == 200
    async with client.factory() as session:
        run = await session.get(Run, 1)
        assert run is not None, "the measurement is kept"
        assert run.machine_id is None, "only the machine link is dropped"


# -- the guard ----------------------------------------------------------------


async def test_active_or_draft_campaign_blocks_edit_and_delete(client):
    for status in (CampaignStatus.ACTIVE.value, CampaignStatus.DRAFT.value,
                   CampaignStatus.SCHEDULED.value, CampaignStatus.PAUSED.value):
        m = await _machine(client, f"nv-{status}")
        await _pin_campaign(client.factory, f"nv-{status}", status)

        put = await client.put(f"/api/machines/{m['id']}",
                               json={"name": f"nv-{status}", "host": "h", "gpu_count": 2})
        assert put.status_code == 409, status
        assert f"camp-{status}" in put.json()["detail"]

        delete = await client.delete(f"/api/machines/{m['id']}")
        assert delete.status_code == 409, status
        assert "active/draft" in delete.json()["detail"]


async def test_a_finished_campaign_does_not_block(client):
    m = await _machine(client, "node-1")
    await _pin_campaign(client.factory, "node-1", CampaignStatus.DONE.value)
    assert (await client.delete(f"/api/machines/{m['id']}")).status_code == 200


async def test_a_campaign_pinned_elsewhere_does_not_block(client):
    m = await _machine(client, "node-1")
    await _machine(client, "node-2")
    await _pin_campaign(client.factory, "node-2", CampaignStatus.ACTIVE.value)
    # The active campaign names node-2, not this machine — node-1 is free to go.
    assert (await client.delete(f"/api/machines/{m['id']}")).status_code == 200


async def test_a_reserved_machine_cannot_be_removed(client):
    m = await _machine(client, "node-1")
    async with client.factory() as session:
        machine = await session.get(Machine, m["id"])
        machine.state = MachineState.RESERVED.value
        await session.commit()
    resp = await client.delete(f"/api/machines/{m['id']}")
    assert resp.status_code == 409
    assert "live run" in resp.json()["detail"]
