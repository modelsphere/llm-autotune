"""Node groups: the multi-node resource, and the contract that it is a topology
rather than a reservation.

The behaviour worth protecting is as much about what these endpoints do NOT do:
forming a group leaves every member an ordinary machine in the single-node
fleet, an unleased member is a blocker rather than a refusal, and dissolving a
group never touches the machines.
"""

from itertools import count

import httpx
import pytest_asyncio
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.core.auth import create_token
from app.db.base import Base, get_async_session
from app.db.models import (
    BaselineStatus,
    Campaign,
    LeaseState,
    Machine,
    MachineState,
    User,
)
from app.main import app

_counter = count()


@pytest_asyncio.fixture
async def client(monkeypatch):
    name = f"mg_{next(_counter)}"
    db = f"sqlite+aiosqlite:///file:{name}?mode=memory&cache=shared&uri=true"
    # The group endpoints read their derived fields (deployable, blockers,
    # busy cards) through the WORKER's sync predicates, deliberately — so they
    # cannot promise a deployment the scheduler would refuse. That means the
    # test has to point the sync factory at the same database as the async one.
    sync_db = f"sqlite:///file:{name}?mode=memory&cache=shared&uri=true"

    engine = create_async_engine(db)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    # NullPool + check_same_thread: the endpoints run these reads in a worker
    # thread (`to_thread.run_sync`), and a pooled sqlite connection would be
    # opened in one thread and reused in another.
    sync_engine = create_engine(
        sync_db, poolclass=NullPool, connect_args={"check_same_thread": False}
    )
    sync_factory = sessionmaker(sync_engine, expire_on_commit=False)
    monkeypatch.setattr(
        "app.api.machine_groups.sync_session_factory", sync_factory
    )

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
    sync_engine.dispose()


async def _machine(client, name="node-1", **over) -> dict:
    body = {"name": name, "host": "10.0.0.1", "gpu_count": 8, **over}
    return (await client.post("/api/machines", json=body)).json()


async def _ready(factory, *names: str) -> None:
    """A leased, handed-over machine: the state a group is actually deployed in."""
    async with factory() as session:
        for row in (await session.execute(select(Machine))).scalars():
            if row.name in names:
                row.state = MachineState.AVAILABLE.value
                row.lease_state = LeaseState.ACTIVE.value
                row.baseline_status = BaselineStatus.CLEARED.value
                row.leased_at = row.created_at
        await session.commit()


async def _group(client, name="pair", members=("node-1", "node-2"), **over):
    return await client.post(
        "/api/machine-groups", json={"name": name, "members": list(members), **over}
    )


# -- creating -----------------------------------------------------------------


async def test_members_keep_the_given_rank_order_and_index_zero_is_master(client):
    await _machine(client, "node-1")
    await _machine(client, "node-2")
    resp = await _group(client)
    assert resp.status_code == 201
    body = resp.json()
    assert [m["name"] for m in body["members"]] == ["node-1", "node-2"]
    assert [m["rank"] for m in body["members"]] == [0, 1]
    assert [m["is_master"] for m in body["members"]] == [True, False]
    assert body["node_count"] == 2


async def test_an_unregistered_member_is_refused(client):
    await _machine(client, "node-1")
    resp = await _group(client, members=("node-1", "ghost"))
    assert resp.status_code == 422
    assert "ghost" in resp.json()["detail"]


async def test_a_machine_has_one_rank_so_duplicates_are_refused(client):
    await _machine(client, "node-1")
    resp = await _group(client, members=("node-1", "node-1"))
    assert resp.status_code == 422
    assert "duplicate" in resp.json()["detail"]


async def test_a_machine_can_belong_to_only_one_group(client):
    await _machine(client, "node-1")
    await _machine(client, "node-2")
    await _machine(client, "node-3")
    assert (await _group(client, "a", ("node-1", "node-2"))).status_code == 201
    resp = await _group(client, "b", ("node-1", "node-3"))
    assert resp.status_code == 409
    assert "only one node group" in resp.json()["detail"]


async def test_a_group_must_be_one_card_type_and_count(client):
    await _machine(client, "node-1", gpu_type="A100", gpu_count=8)
    await _machine(client, "node-2", gpu_type="H100", gpu_count=8)
    resp = await _group(client)
    assert resp.status_code == 422
    assert "one GPU type" in resp.json()["detail"]

    await _machine(client, "node-3", gpu_type="A100", gpu_count=4)
    resp = await _group(client, "counts", ("node-1", "node-3"))
    assert resp.status_code == 422
    assert "same card count" in resp.json()["detail"]


# -- the topology, not a reservation ------------------------------------------


async def test_an_unleased_member_is_a_blocker_not_a_refusal(client):
    """A lease is nightly and a group is a standing topology. Forcing the group
    to be rebuilt every evening would be worse than telling the operator what is
    missing, so this is a 201 with a named blocker."""
    await _machine(client, "node-1")
    await _machine(client, "node-2")
    body = (await _group(client)).json()
    assert body["deployable"] is False
    assert any("not leased" in blocker for blocker in body["blockers"])


async def test_a_leased_handed_over_pair_is_deployable(client):
    await _machine(client, "node-1")
    await _machine(client, "node-2")
    await _ready(client.factory, "node-1", "node-2")
    body = (await _group(client)).json()
    assert body["deployable"] is True
    assert body["blockers"] == []
    assert all(m["leased"] for m in body["members"])


async def test_forming_a_group_leaves_the_machines_schedulable(client):
    """The whole contract in one test: grouping is not a reservation. The
    machines stay in the fleet list, still available, and nothing about their
    state changed."""
    await _machine(client, "node-1")
    await _machine(client, "node-2")
    await _ready(client.factory, "node-1", "node-2")
    await _group(client)
    machines = {m["name"]: m for m in (await client.get("/api/machines")).json()}
    assert machines["node-1"]["state"] == MachineState.AVAILABLE.value
    assert machines["node-1"]["group"] == "pair"
    assert machines["node-1"]["group_rank"] == 0
    assert machines["node-2"]["group_rank"] == 1


async def test_deleting_a_group_never_touches_the_machines(client):
    await _machine(client, "node-1")
    await _machine(client, "node-2")
    await _ready(client.factory, "node-1", "node-2")
    created = (await _group(client)).json()
    assert (await client.delete(f"/api/machine-groups/{created['id']}")).status_code == 200

    machines = (await client.get("/api/machines")).json()
    assert {m["name"] for m in machines} == {"node-1", "node-2"}
    assert all(m["group"] == "" for m in machines)
    assert (await client.get("/api/machine-groups")).json() == []


async def test_deleting_is_refused_while_a_campaign_deploys_across_it(client):
    await _machine(client, "node-1")
    await _machine(client, "node-2")
    created = (await _group(client)).json()
    async with client.factory() as session:
        session.add(Campaign(
            owner_id=1, name="c", engine="sglang", image="i", model_path="/m",
            served_model_name="m", search_space={}, node_group="pair", status="draft",
        ))
        await session.commit()
    resp = await client.delete(f"/api/machine-groups/{created['id']}")
    assert resp.status_code == 409
    assert "campaign" in resp.json()["detail"]


# -- editing ------------------------------------------------------------------


async def test_replacing_members_re_ranks_the_set(client):
    await _machine(client, "node-1")
    await _machine(client, "node-2")
    await _machine(client, "node-3")
    created = (await _group(client, members=("node-1", "node-2"))).json()
    edited = await client.put(
        f"/api/machine-groups/{created['id']}", json={"members": ["node-3", "node-1"]}
    )
    assert edited.status_code == 200
    body = edited.json()
    assert [m["name"] for m in body["members"]] == ["node-3", "node-1"]
    assert body["members"][0]["is_master"] is True
    # node-2 was released by the edit, so it is available to another group.
    assert (await _group(client, "other", ("node-2",))).status_code == 201


async def test_notes_and_network_settings_round_trip(client):
    await _machine(client, "node-1")
    resp = await _group(
        client,
        members=("node-1",),
        driver="ssh_docker",
        nccl_env={"NCCL_SOCKET_IFNAME": "ib0"},
        dist_port=29500,
        notes="rack 4",
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["nccl_env"] == {"NCCL_SOCKET_IFNAME": "ib0"}
    assert body["dist_port"] == 29500
    assert body["notes"] == "rack 4"


# -- the machine address columns ----------------------------------------------


async def test_data_host_round_trips_and_defaults_to_host(client):
    plain = await _machine(client, "node-1")
    assert plain["data_host"] == ""
    assert plain["nccl_ifname"] == ""

    rail = await _machine(
        client, "node-2", host="10.0.0.2", data_host="192.168.9.2", nccl_ifname="ib0"
    )
    assert rail["data_host"] == "192.168.9.2"
    assert rail["nccl_ifname"] == "ib0"


async def test_a_group_reports_each_members_interior_address(client):
    await _machine(client, "node-1", host="10.0.0.1", data_host="192.168.9.1")
    await _machine(client, "node-2", host="10.0.0.2")  # no rail: falls back to host
    body = (await _group(client)).json()
    by_name = {m["name"]: m for m in body["members"]}
    assert by_name["node-1"]["data_host"] == "192.168.9.1"
    assert by_name["node-2"]["data_host"] == "10.0.0.2"


# -- campaigns pinning a group ------------------------------------------------


def _campaign_body(**over) -> dict:
    return {
        "name": "c", "engine": "sglang", "image": "img", "model_path": "/m",
        "served_model_name": "m", "search_space": {"grid": {"tp_size": [2]}},
        **over,
    }


async def test_a_campaign_pinning_an_unknown_group_is_refused(client):
    resp = await client.post("/api/campaigns", json=_campaign_body(node_group="nope"))
    assert resp.status_code == 422
    assert "no node group named" in resp.json()["detail"]


async def test_vllm_cannot_pin_a_group(client):
    await _machine(client, "node-1")
    await _machine(client, "node-2")
    await _group(client)
    resp = await client.post(
        "/api/campaigns", json=_campaign_body(engine="vllm", node_group="pair")
    )
    assert resp.status_code == 422
    assert "vllm" in resp.json()["detail"].lower()


async def test_a_group_pinned_campaign_is_stored_and_can_be_started(client):
    """The pin is real (stored, validated, returned) and, now that the
    multi-node launch exists, starting is no longer refused — the scheduler
    places the whole group or waits for it."""
    await _machine(client, "node-1")
    await _machine(client, "node-2")
    await _group(client)
    created = await client.post("/api/campaigns", json=_campaign_body(node_group="pair"))
    assert created.status_code == 200
    campaign = created.json()
    assert campaign["node_group"] == "pair"

    resp = await client.put(
        f"/api/campaigns/{campaign['id']}/status", json={"status": "active"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "active"


async def test_a_single_node_campaign_is_unaffected(client):
    await _machine(client, "node-1")
    created = await client.post("/api/campaigns", json=_campaign_body())
    assert created.status_code == 200
    assert created.json()["node_group"] == ""
    resp = await client.put(
        f"/api/campaigns/{created.json()['id']}/status", json={"status": "active"}
    )
    assert resp.status_code == 200


async def test_preflight_reports_the_group_and_probes_only_its_members(client):
    """The wizard must see the deployment shape before anything is saved, and
    must probe only the group's machines rather than the whole fleet."""
    await _machine(client, "node-1")
    await _machine(client, "node-2")
    await _machine(client, "node-3")
    await _group(client, members=("node-1", "node-2"))
    resp = await client.post(
        "/api/campaigns/preflight", json=_campaign_body(node_group="pair")
    )
    assert resp.status_code == 200
    body = resp.json()
    assert {row["machine"] for row in body["machines"]} == {"node-1", "node-2"}
    for row in body["machines"]:
        check = next(c for c in row["checks"] if c["key"] == "multi_node")
        assert check["status"] == "pass"
        assert "pair" in check["detail"]


# -- group preflight ----------------------------------------------------------


class _ProbeResult:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


async def test_group_preflight_probes_every_member_and_the_interior_link(client, monkeypatch):
    """The whole point of the group preflight: a per-machine view cannot see
    that two individually-healthy boxes cannot reach each other."""
    await _machine(client, "node-1", host="10.0.0.1", data_host="192.168.9.1")
    await _machine(client, "node-2", host="10.0.0.2")
    group = (await _group(client)).json()

    scripts: dict[str, str] = {}

    class _Driver:
        def _ssh(self, machine, script, timeout=45):
            scripts[machine.name] = script
            out = "ssh ok\nports 22 80\nportholder\nkube no\ngpus 8\nib 2\n"
            if "ping -c1 -W2" in script:
                out += "peer ok 192.168.9.1\n"
            return _ProbeResult(out)

    monkeypatch.setattr("app.api.machine_groups.get_driver", lambda name: _Driver())
    resp = await client.post(f"/api/machine-groups/{group['id']}/preflight", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert {row["machine"] for row in body["machines"]} == {"node-1", "node-2"}
    assert body["ok"] is True
    # Every member is probed, and the worker is told to reach the MASTER's
    # interior address — the master is not asked to ping itself.
    assert "ping -c1 -W2 192.168.9.1" in scripts["node-2"]
    assert "ping -c1 -W2" not in scripts["node-1"]
    for row in body["machines"]:
        parity = next(c for c in row["checks"] if c["key"] == "homogeneity")
        assert parity["status"] == "pass"


async def test_group_preflight_without_an_image_does_not_claim_it_is_missing(client, monkeypatch):
    """A fabric-only check must not report "image absent" on every member —
    the image was never given, so that check was never asked."""
    await _machine(client, "node-1")
    await _machine(client, "node-2")
    group = (await _group(client)).json()

    class _Driver:
        def _ssh(self, machine, script, timeout=45):
            assert "docker image inspect" not in script
            return _ProbeResult("ssh ok\nports 22\nportholder\nkube no\ngpus 8\nib 2\n")

    monkeypatch.setattr("app.api.machine_groups.get_driver", lambda name: _Driver())
    resp = await client.post(f"/api/machine-groups/{group['id']}/preflight", json={})
    assert resp.status_code == 200
    keys = {c["key"] for row in resp.json()["machines"] for c in row["checks"]}
    assert "image" not in keys
