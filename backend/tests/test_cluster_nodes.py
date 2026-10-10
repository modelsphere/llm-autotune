"""Adding a GPU cluster registers its GPU nodes as machines.

The kubeconfig deploy/gpu-cluster.sh writes names its namespace, so adding a
cluster needs nothing else; the platform then lists the cluster's GPU nodes,
and each one registered becomes a machine pinned to that node by hostname, with
the cards the node reports, not yet leased.
"""

import pytest

from app.api import clusters as clusters_api
from app.db.models import Machine
from tests.test_clusters_api import KUBECONFIG, client  # noqa: F401  (fixture)


def _node(name, gpus=8, product="NVIDIA-H800", ready=True, cordoned=False, hostname=None):
    return {
        "metadata": {"name": name, "labels": {
            "kubernetes.io/hostname": hostname or name,
            **({"nvidia.com/gpu.product": product} if product else {}),
        }},
        "spec": {"unschedulable": True} if cordoned else {},
        "status": {
            "allocatable": {"nvidia.com/gpu": str(gpus)} if gpus else {},
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
        },
    }


@pytest.fixture
def cluster_nodes(monkeypatch):
    nodes = [_node("h800-37"), _node("h800-40", cordoned=True),
             _node("cpu-1", gpus=0, product=None), _node("a100-5", product="NVIDIA-A100-SXM4-80GB")]

    class _Api:
        def list(self, kind, **_):
            assert kind == "nodes"
            return nodes

    monkeypatch.setattr(clusters_api, "get_k8s_api", lambda cluster: _Api())
    return nodes


async def _add(client):  # noqa: F811
    response = await client.post("/api/clusters", json={"name": "test", "kubeconfig": KUBECONFIG})
    assert response.status_code == 201, response.text
    return response.json()


async def test_the_namespace_comes_from_the_kubeconfig(client):  # noqa: F811
    assert (await _add(client))["namespace"] == "gpu-test"


async def test_a_kubeconfig_naming_no_namespace_needs_one(client):  # noqa: F811
    bare = KUBECONFIG.replace(", namespace: gpu-test", "")
    response = await client.post("/api/clusters", json={"name": "t", "kubeconfig": bare})
    assert response.status_code == 422
    assert "namespace" in response.json()["detail"]


async def test_only_gpu_nodes_are_listed(client, cluster_nodes):  # noqa: F811
    cluster = await _add(client)
    view = (await client.get(f"/api/clusters/{cluster['id']}/nodes")).json()
    rows = {n["node"]: n for n in view["nodes"]}
    assert set(rows) == {"a100-5", "h800-37", "h800-40"}
    assert rows["h800-37"]["gpu_count"] == 8 and rows["h800-37"]["gpu_type"]
    assert rows["h800-40"]["schedulable"] is False
    assert all(n["machine"] is None for n in view["nodes"])


async def test_registering_makes_one_machine_per_node(client, cluster_nodes):  # noqa: F811
    cluster = await _add(client)
    response = await client.post(f"/api/clusters/{cluster['id']}/nodes",
                                 json={"nodes": ["h800-37", "a100-5"]})
    assert response.status_code == 200, response.text
    assert sorted(response.json()["registered"]) == ["a100-5", "h800-37"]
    async with client.factory() as session:
        machine = (await session.execute(
            Machine.__table__.select().where(Machine.name == "h800-37"))).one()
    assert machine.driver == "k8s" and machine.cluster_id == cluster["id"]
    assert machine.node_selector == "kubernetes.io/hostname=h800-37"
    assert machine.gpu_count == 8 and machine.state == "away"

    again = await client.post(f"/api/clusters/{cluster['id']}/nodes", json={"nodes": ["h800-37"]})
    assert again.json()["registered"] == [], "a registered node is not registered twice"
    listed = {n["node"]: n["machine"] for n in again.json()["nodes"]}
    assert listed["h800-37"]["name"] == "h800-37" and listed["h800-40"] is None


async def test_a_node_that_left_is_reported(client, cluster_nodes):  # noqa: F811
    cluster = await _add(client)
    await client.post(f"/api/clusters/{cluster['id']}/nodes", json={"nodes": ["h800-37"]})
    cluster_nodes.pop(0)
    view = (await client.get(f"/api/clusters/{cluster['id']}/nodes")).json()
    assert [g["name"] for g in view["gone"]] == ["h800-37"]


async def test_a_taken_name_gets_the_cluster_prefix(client, cluster_nodes):  # noqa: F811
    async with client.factory() as session:
        session.add(Machine(name="h800-37", host="10.0.0.1", gpu_count=8))
        await session.commit()
    cluster = await _add(client)
    response = await client.post(f"/api/clusters/{cluster['id']}/nodes",
                                 json={"nodes": ["h800-37"]})
    assert response.json()["registered"] == ["test-h800-37"]


async def test_a_node_not_in_the_cluster_is_refused(client, cluster_nodes):  # noqa: F811
    cluster = await _add(client)
    response = await client.post(f"/api/clusters/{cluster['id']}/nodes", json={"nodes": ["cpu-1"]})
    assert response.status_code == 422
