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
    nodes = [_node("gpu-node-1"), _node("gpu-node-2", cordoned=True),
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
    assert set(rows) == {"a100-5", "gpu-node-1", "gpu-node-2"}
    assert rows["gpu-node-1"]["gpu_count"] == 8 and rows["gpu-node-1"]["gpu_type"]
    assert rows["gpu-node-2"]["schedulable"] is False
    assert all(n["machine"] is None for n in view["nodes"])


async def test_registering_makes_one_machine_per_node(client, cluster_nodes):  # noqa: F811
    cluster = await _add(client)
    response = await client.post(f"/api/clusters/{cluster['id']}/nodes",
                                 json={"nodes": ["gpu-node-1", "a100-5"]})
    assert response.status_code == 200, response.text
    assert sorted(response.json()["registered"]) == ["a100-5", "gpu-node-1"]
    async with client.factory() as session:
        machine = (await session.execute(
            Machine.__table__.select().where(Machine.name == "gpu-node-1"))).one()
    assert machine.driver == "k8s" and machine.cluster_id == cluster["id"]
    assert machine.node_selector == "kubernetes.io/hostname=gpu-node-1"
    assert machine.gpu_count == 8 and machine.state == "away"

    again = await client.post(
        f"/api/clusters/{cluster['id']}/nodes", json={"nodes": ["gpu-node-1"]})
    assert again.json()["registered"] == [], "a registered node is not registered twice"
    listed = {n["node"]: n["machine"] for n in again.json()["nodes"]}
    assert listed["gpu-node-1"]["name"] == "gpu-node-1" and listed["gpu-node-2"] is None


async def test_a_node_that_left_is_reported(client, cluster_nodes):  # noqa: F811
    cluster = await _add(client)
    await client.post(f"/api/clusters/{cluster['id']}/nodes", json={"nodes": ["gpu-node-1"]})
    cluster_nodes.pop(0)
    view = (await client.get(f"/api/clusters/{cluster['id']}/nodes")).json()
    assert [g["name"] for g in view["gone"]] == ["gpu-node-1"]


async def test_a_taken_name_gets_the_cluster_prefix(client, cluster_nodes):  # noqa: F811
    async with client.factory() as session:
        session.add(Machine(name="gpu-node-1", host="10.0.0.1", gpu_count=8))
        await session.commit()
    cluster = await _add(client)
    response = await client.post(f"/api/clusters/{cluster['id']}/nodes",
                                 json={"nodes": ["gpu-node-1"]})
    assert response.json()["registered"] == ["test-gpu-node-1"]


async def test_a_node_not_in_the_cluster_is_refused(client, cluster_nodes):  # noqa: F811
    cluster = await _add(client)
    response = await client.post(f"/api/clusters/{cluster['id']}/nodes", json={"nodes": ["cpu-1"]})
    assert response.status_code == 422


async def test_a_gpu_type_registers_as_one_pool_of_its_nodes(client, cluster_nodes):  # noqa: F811
    cluster = await _add(client)
    view = (await client.get(f"/api/clusters/{cluster['id']}/nodes")).json()
    pools = {p["product"]: p for p in view["pools"]}
    assert set(pools) == {"NVIDIA-H800", "NVIDIA-A100-SXM4-80GB"}
    h800 = pools["NVIDIA-H800"]
    assert sorted(h800["nodes"]) == ["gpu-node-1", "gpu-node-2"]
    # The cordoned node's cards are shown but are not capacity.
    assert (h800["gpu_count"], h800["gpu_total"], h800["nodes_usable"]) == (8, 16, 1)
    assert h800["node_gpu_max"] == 8

    response = await client.post(f"/api/clusters/{cluster['id']}/pools",
                                 json={"products": ["NVIDIA-H800"]})
    assert response.status_code == 200, response.text
    assert response.json()["registered"] == ["test-h800"]
    async with client.factory() as session:
        machine = (await session.execute(
            Machine.__table__.select().where(Machine.name == "test-h800"))).one()
    assert machine.node_selector == "nvidia.com/gpu.product=NVIDIA-H800"
    assert (machine.gpu_count, machine.node_count, machine.node_gpu_max) == (8, 2, 8)
    assert machine.state == "away"  # registered, not leased

    # Registered once: listed against its pool, and skipped when asked again.
    view = (await client.get(f"/api/clusters/{cluster['id']}/nodes")).json()
    assert {p["product"]: p["machine"] for p in view["pools"]}["NVIDIA-H800"]["name"] == "test-h800"
    again = await client.post(f"/api/clusters/{cluster['id']}/pools",
                              json={"products": ["NVIDIA-H800"]})
    assert again.json()["registered"] == []

    unknown = await client.post(f"/api/clusters/{cluster['id']}/pools",
                                json={"products": ["NVIDIA-GB200"]})
    assert unknown.status_code == 422


async def test_npu_cluster_round_trips_label_and_registers_nodes_and_pools(
    client, cluster_nodes,  # noqa: F811
):
    label = "node.kubernetes.io/npu.chip.name"
    for node in cluster_nodes[:2]:
        node["metadata"]["labels"] = {label: "910B4-1"}
        node["status"]["allocatable"] = {"huawei.com/npu": "8"}
    response = await client.post("/api/clusters", json={
        "name": "ascend", "kubeconfig": KUBECONFIG,
        "gpu_resource": "huawei.com/npu", "gpu_product_label": label, "runtime_class": "",
    })
    assert response.status_code == 201, response.text
    cluster = response.json()
    assert cluster["gpu_product_label"] == label
    assert cluster["runtime_class"] == ""
    cid = cluster["id"]
    listed = (await client.get(f"/api/clusters/{cid}/nodes")).json()
    npu = next(n for n in listed["nodes"] if n["node"] == "gpu-node-1")
    assert npu["gpu_count"] == 8 and npu["gpu_type"] == "ASCEND-910B4-1"
    probe = (await client.post(f"/api/clusters/{cid}/probe")).json()
    assert "ASCEND-910B4-1" in probe["probe"]["gpu_types"]

    response = await client.post(f"/api/clusters/{cid}/nodes", json={"nodes": ["gpu-node-1"]})
    assert response.status_code == 200, response.text
    async with client.factory() as session:
        machine = (await session.execute(
            Machine.__table__.select().where(Machine.name == "gpu-node-1")
        )).one()
        assert machine.gpu_type == "ASCEND-910B4-1" and machine.gpu_count == 8
        # Gone again: a pool over a node registered alone would count it twice.
        await session.execute(Machine.__table__.delete().where(Machine.name == "gpu-node-1"))
        await session.commit()

    response = await client.post(f"/api/clusters/{cid}/pools", json={"products": ["910B4-1"]})
    assert response.status_code == 200, response.text
    pool = next(p for p in response.json()["pools"] if p["product"] == "910B4-1")
    assert pool["machine"] is not None
    async with client.factory() as session:
        machine = (await session.execute(
            Machine.__table__.select().where(Machine.name == pool["machine"]["name"])
        )).one()
    assert machine.node_selector == f"{label}=910B4-1"
    # gpu-node-2 is cordoned: its cards are not capacity.
    assert (machine.gpu_count, machine.node_gpu_max) == (8, 8)
    again = await client.post(f"/api/clusters/{cid}/pools", json={"products": ["910B4-1"]})
    assert again.json()["registered"] == []


async def test_a_node_inside_a_registered_pool_cannot_register_alone(client, cluster_nodes):  # noqa: F811
    cluster = await _add(client)
    await client.post(f"/api/clusters/{cluster['id']}/pools", json={"products": ["NVIDIA-H800"]})
    view = (await client.get(f"/api/clusters/{cluster['id']}/nodes")).json()
    rows = {n["node"]: n for n in view["nodes"]}
    assert rows["gpu-node-1"]["pool"]["name"] == "test-h800"
    assert rows["a100-5"]["pool"] is None
    response = await client.post(f"/api/clusters/{cluster['id']}/nodes",
                                 json={"nodes": ["gpu-node-1"]})
    assert response.status_code == 409 and "count twice" in response.json()["detail"]


async def test_a_pool_over_nodes_registered_alone_is_refused(client, cluster_nodes):  # noqa: F811
    cluster = await _add(client)
    await client.post(f"/api/clusters/{cluster['id']}/nodes", json={"nodes": ["gpu-node-1"]})
    view = (await client.get(f"/api/clusters/{cluster['id']}/nodes")).json()
    pools = {p["product"]: p for p in view["pools"]}
    assert pools["NVIDIA-H800"]["single_nodes"] == ["gpu-node-1"]
    response = await client.post(f"/api/clusters/{cluster['id']}/pools",
                                 json={"products": ["NVIDIA-H800"]})
    assert response.status_code == 409 and "gpu-node-1" in response.json()["detail"]


async def test_two_products_of_one_card_type_get_distinct_pool_names(client, monkeypatch):  # noqa: F811
    nodes = [_node("h-1", product="NVIDIA-H100-80GB-HBM3"), _node("h-2", product="NVIDIA-H100-SXM")]

    class _Api:
        def list(self, kind, **_):
            return nodes

    monkeypatch.setattr(clusters_api, "get_k8s_api", lambda cluster: _Api())
    cluster = await _add(client)
    response = await client.post(f"/api/clusters/{cluster['id']}/pools",
                                 json={"products": ["NVIDIA-H100-80GB-HBM3", "NVIDIA-H100-SXM"]})
    assert response.status_code == 200, response.text
    assert response.json()["registered"] == ["test-h100", "test-nvidia-h100-sxm"]


async def test_without_node_access_the_namespace_quota_is_one_pool(client, monkeypatch):  # noqa: F811
    class _Api:
        def list(self, kind, **_):
            if kind == "nodes":
                raise RuntimeError('nodes is forbidden: User "x" cannot list resource "nodes"')
            if kind == "resourcequotas":
                return [{"status": {"hard": {"requests.nvidia.com/gpu": "16"},
                                    "used": {"requests.nvidia.com/gpu": "4"}}}]
            return []

    monkeypatch.setattr(clusters_api, "get_k8s_api", lambda cluster: _Api())
    monkeypatch.setattr(clusters_api.K8sDriver, "api", property(lambda self: _Api()))
    cluster = await _add(client)
    view = (await client.get(f"/api/clusters/{cluster['id']}/nodes")).json()
    assert view["nodes_readable"] is False and view["quota"] == {"gpus": 16, "used": 4}
    response = await client.post(f"/api/clusters/{cluster['id']}/pools", json={"namespace": True})
    assert response.status_code == 200, response.text
    assert response.json()["registered"] == ["test"]
    assert response.json()["namespace_machine"]["name"] == "test"
    async with client.factory() as session:
        machine = (await session.execute(
            Machine.__table__.select().where(Machine.name == "test"))).one()
    assert (machine.node_selector, machine.gpu_count, machine.gpu_free) == ("", 16, 12)
    again = await client.post(f"/api/clusters/{cluster['id']}/pools", json={"namespace": True})
    assert again.status_code == 409


async def test_a_checked_add_keeps_only_a_cluster_it_can_reach(client, monkeypatch):  # noqa: F811
    probe = {"reachable": False, "warnings": ["cannot list pods: connection refused"]}
    monkeypatch.setattr(clusters_api, "_probe", lambda cluster: dict(probe))
    body = {"name": "test", "kubeconfig": KUBECONFIG}
    refused = await client.post("/api/clusters?check=true", json=body)
    assert refused.status_code == 422
    assert refused.json()["detail"]["probe"]["warnings"] == probe["warnings"]
    assert (await client.get("/api/clusters")).json() == []

    probe.update(reachable=True, crd_present=False, warnings=["llmservices cannot be listed"])
    added = await client.post("/api/clusters?check=true", json=body)
    assert added.status_code == 201, added.text
    assert added.json()["last_probe"]["crd_present"] is False
