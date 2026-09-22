"""The clusters API: write-only kubeconfig, encrypted at rest, and the guards
that keep a cluster in use from being removed."""

from itertools import count

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.auth import create_token
from app.core.secrets import decrypt_secret
from app.db.base import Base, get_async_session
from app.db.models import Cluster, Machine, User
from app.main import app

_counter = count()


@pytest_asyncio.fixture
async def client():
    name = f"cl_{next(_counter)}"
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


KUBECONFIG = """
apiVersion: v1
kind: Config
clusters:
- cluster: {server: https://cluster-a.example:6443, insecure-skip-tls-verify: true}
  name: c
contexts:
- context: {cluster: c, user: u, namespace: gpu-test}
  name: ctx
current-context: ctx
users:
- name: u
  user: {token: super-secret-token}
"""


async def _add(client, name="gpu-cluster-a", **over) -> dict:
    body = {
        "name": name,
        "kubeconfig": KUBECONFIG,
        "namespace": "gpu-test",
        "workload_kind": "deployment",
        "node_host": "198.51.100.10",
        **over,
    }
    return (await client.post("/api/clusters", json=body)).json()


async def test_create_stores_encrypted_and_never_returns_the_kubeconfig(client):
    created = await _add(client)
    assert created["name"] == "gpu-cluster-a"
    assert created["has_kubeconfig"] is True
    assert "kubeconfig" not in created and "kubeconfig_enc" not in created

    async with client.factory() as session:
        row = await session.get(Cluster, created["id"])
        assert row.kubeconfig_enc != KUBECONFIG
        assert "super-secret-token" not in row.kubeconfig_enc
        assert decrypt_secret(row.kubeconfig_enc) == KUBECONFIG

    listed = (await client.get("/api/clusters")).json()
    assert [c["name"] for c in listed] == ["gpu-cluster-a"]
    assert all("kubeconfig" not in c for c in listed)


async def test_update_with_empty_kubeconfig_keeps_the_stored_one(client):
    created = await _add(client)
    body = {
        "name": "gpu-cluster-a", "kubeconfig": "", "namespace": "gpu-test",
        "workload_kind": "deployment",
    }
    edited = (await client.put(f"/api/clusters/{created['id']}", json=body)).json()
    assert edited["has_kubeconfig"] is True
    async with client.factory() as session:
        stored = decrypt_secret((await session.get(Cluster, created["id"])).kubeconfig_enc)
    assert stored == KUBECONFIG


async def test_update_with_a_new_kubeconfig_rotates_it(client):
    created = await _add(client)
    body = {
        "name": "gpu-cluster-a", "kubeconfig": KUBECONFIG.replace("super-secret", "rotated"),
        "namespace": "gpu-test", "workload_kind": "deployment",
    }
    await client.put(f"/api/clusters/{created['id']}", json=body)
    async with client.factory() as session:
        stored = decrypt_secret((await session.get(Cluster, created["id"])).kubeconfig_enc)
    assert "rotated" in stored


async def test_machine_carries_its_cluster(client):
    created = await _add(client)
    body = {"name": "b300-k8s", "host": "k8s", "driver": "", "cluster_id": created["id"]}
    machine = (await client.post("/api/machines", json=body)).json()
    assert machine["cluster_id"] == created["id"]
    assert machine["cluster_name"] == "gpu-cluster-a"

    listed = (await client.get("/api/machines")).json()
    assert listed[0]["cluster_name"] == "gpu-cluster-a"


async def test_delete_refused_while_a_machine_names_the_cluster(client):
    created = await _add(client)
    await client.post(
        "/api/machines",
        json={"name": "b300-k8s", "host": "k8s", "cluster_id": created["id"]},
    )
    refused = await client.delete(f"/api/clusters/{created['id']}")
    assert refused.status_code == 409
    assert "still name it" in refused.json()["detail"]

    await client.delete("/api/machines/1")
    assert (await client.delete(f"/api/clusters/{created['id']}")).status_code == 200


async def test_probe_reports_unavailable_without_touching_a_cluster(client):
    created = await _add(client, api_mode="unavailable")
    resp = await client.post(f"/api/clusters/{created['id']}/probe")
    assert resp.status_code == 200
    probe = resp.json()["probe"]
    assert probe["reachable"] is False
    assert any("unavailable" in w for w in probe["warnings"])
    # The result is stored so the page need not re-probe on every load.
    assert (await client.get("/api/clusters")).json()[0]["last_probe"]["api_mode"] == "unavailable"


async def test_a_group_may_not_span_two_clusters(client):
    a = await _add(client, name="cluster-a")
    b = await _add(client, name="cluster-b")
    for name, cid in (("m-a", a["id"]), ("m-b", b["id"])):
        await client.post(
            "/api/machines",
            json={"name": name, "host": "10.0.0.1", "gpu_type": "B300", "cluster_id": cid},
        )
    resp = await client.post(
        "/api/machine-groups", json={"name": "gang", "members": ["m-a", "m-b"]}
    )
    assert resp.status_code == 422
    assert "one cluster" in resp.json()["detail"]


async def test_create_requires_admin(client):
    async with client.factory() as session:
        session.add(User(id=2, username="bob", password_hash="x", role="user"))
        await session.commit()
        token = create_token(await session.get(User, 2))
    resp = await client.post(
        "/api/clusters",
        json={"name": "nope", "kubeconfig": KUBECONFIG},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_a_machine_row_defaults_to_the_default_cluster(client):
    """cluster_id is optional: an ssh machine (and every machine that predates
    clusters) has no cluster and is served by the default settings."""
    machine = (
        await client.post("/api/machines", json={"name": "node-1", "host": "10.0.0.1"})
    ).json()
    assert machine["cluster_id"] is None
    assert machine["cluster_name"] == ""

    async with client.factory() as session:
        assert (await session.get(Machine, 1)).cluster_id is None
