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
    MachineGroup,
    MachineGroupMember,
    MachineState,
    Policy,
    PolicySession,
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


async def test_a_machine_needs_a_name_and_an_ssh_machine_a_host(client):
    """An empty Add machine form used to create a nameless, hostless machine."""
    blank = await client.post("/api/machines", json={"name": "  ", "host": "", "gpu_count": 8})
    assert blank.status_code == 422
    assert "name" in str(blank.json()["detail"])
    no_host = await client.post("/api/machines", json={"name": "box", "host": "", "gpu_count": 8})
    assert no_host.status_code == 422
    assert "host" in str(no_host.json()["detail"])
    # A Kubernetes slice is reached through its cluster, not a host.
    k8s = await client.post(
        "/api/machines", json={"name": "slice", "host": "", "driver": "k8s", "gpu_count": 4}
    )
    assert k8s.status_code == 200, k8s.text


async def test_a_machine_whose_cards_cannot_be_read_needs_a_count(client):
    """The form used to send 8 GPUs whatever the machine had; a machine that
    does not answer now has to be given its count."""
    unread = await client.post("/api/machines", json={"name": "box", "host": "10.0.0.9"})
    assert unread.status_code == 422
    assert "GPU count" in unread.json()["detail"]
    given = await client.post(
        "/api/machines", json={"name": "box", "host": "10.0.0.9", "gpu_count": 4})
    assert given.json()["gpu_count"] == 4
    # An edit that leaves the count out keeps the one stored.
    edited = await client.put(f"/api/machines/{given.json()['id']}",
                              json={"name": "box", "host": "10.0.0.9", "notes": "rack 2"})
    assert edited.status_code == 200, edited.text
    assert edited.json()["gpu_count"] == 4


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
        # A policy campaign's session names the machine its container ran on.
        session.add(Policy(id=1, owner_id=1, name="p", image="i"))
        session.add(PolicySession(id=1, campaign_id=1, policy_id=1, machine_id=m["id"],
                                  status="done"))
        await session.commit()

    assert (await client.delete(f"/api/machines/{m['id']}")).status_code == 200
    async with client.factory() as session:
        run = await session.get(Run, 1)
        assert run is not None, "the measurement is kept"
        assert run.machine_id is None, "only the machine link is dropped"
        assert (await session.get(PolicySession, 1)).machine_id is None


async def test_a_node_group_member_is_taken_out_of_its_group_first(client):
    a = await _machine(client, "node-1")
    await _machine(client, "node-2")
    async with client.factory() as session:
        session.add(MachineGroup(id=1, name="pair"))
        session.add(MachineGroupMember(group_id=1, machine_id=a["id"], rank=0))
        await session.commit()
    resp = await client.delete(f"/api/machines/{a['id']}")
    assert resp.status_code == 409
    assert "node group pair" in resp.json()["detail"]


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


async def test_smoke_test_reports_the_drivers_checks_and_an_unset_card_type(client, monkeypatch):
    from app.control.launch.ssh_docker import SshDockerDriver

    calls = []

    def fake(self, machine, probe_pod=False, progress=None):
        calls.append((machine.name, probe_pod))
        return {"supported": True, "ok": True,
                "checks": [{"name": "ssh", "status": "pass", "detail": "logged in"}]}

    monkeypatch.setattr(SshDockerDriver, "smoke_test", fake)
    m = await _machine(client, gpu_type="")
    body = (await client.post(f"/api/machines/{m['id']}/smoke-test")).json()
    assert body["ok"] is True
    assert [c["name"] for c in body["checks"]] == ["ssh", "card type"]
    assert calls == [("node-1", False)]

    typed = await _machine(client, name="node-2", gpu_type="H100")
    body = (await client.post(f"/api/machines/{typed['id']}/smoke-test?pod=true")).json()
    assert [c["name"] for c in body["checks"]] == ["ssh"]
    assert calls[-1] == ("node-2", True)


async def test_a_crashing_smoke_test_is_a_failed_report_not_a_500(client, monkeypatch):
    from app.control.launch.ssh_docker import SshDockerDriver

    def boom(self, machine, probe_pod=False, progress=None):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(SshDockerDriver, "smoke_test", boom)
    m = await _machine(client, gpu_type="H100")
    resp = await client.post(f"/api/machines/{m['id']}/smoke-test")
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert "kaboom" in resp.json()["checks"][0]["detail"]


async def test_a_streamed_smoke_test_sends_each_step_then_the_report(client, monkeypatch):
    import json

    from app.control.launch.base import SmokeSteps, smoke_check
    from app.control.launch.ssh_docker import SshDockerDriver

    def fake(self, machine, probe_pod=False, progress=None):
        steps = SmokeSteps(["ssh", "docker"], progress)
        steps.run("ssh", lambda: smoke_check("ssh", "pass", "logged in"))
        steps.run("docker", lambda: smoke_check("docker", "fail", "no daemon"))
        return steps.result()

    monkeypatch.setattr(SshDockerDriver, "smoke_test", fake)
    m = await _machine(client, gpu_type="")
    resp = await client.post(f"/api/machines/{m['id']}/smoke-test?stream=true")
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    assert resp.headers["x-accel-buffering"] == "no"
    events = [json.loads(line) for line in resp.text.splitlines()]
    assert events[0] == {"event": "plan", "steps": ["ssh", "docker"]}
    assert [(e["event"], e.get("name")) for e in events[1:-1]] == [
        ("start", "ssh"), ("step", "ssh"), ("start", "docker"), ("step", "docker"),
        ("step", "card type"),
    ]
    done = events[-1]
    assert done["event"] == "done" and done["ok"] is False and done["machine"] == "node-1"
    assert [c["name"] for c in done["checks"]] == ["ssh", "docker", "card type"]
