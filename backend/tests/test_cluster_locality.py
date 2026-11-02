"""Telling AutoTune's own cluster from another one, by CA, and refusing what
cannot work: a run on another cluster named by its Service DNS loads its model
and is then unreachable (Name or service not known)."""

import base64
from types import SimpleNamespace

import pytest

from app.control.launch import locality
from app.control.launch.locality import same_cluster, unreachable_by_name

from .test_clusters_api import _add, client  # noqa: F401 — the fixture


def _pem(seed: bytes) -> bytes:
    body = base64.b64encode(seed * 8).decode()
    return f"-----BEGIN CERTIFICATE-----\n{body}\n-----END CERTIFICATE-----\n".encode()


OWN = _pem(b"own-cluster-ca")
OTHER = _pem(b"another-ca")


def _kubeconfig(ca: bytes | None) -> str:
    tls = (f"certificate-authority-data: {base64.b64encode(ca).decode()}"
           if ca else "insecure-skip-tls-verify: true")
    return f"""
apiVersion: v1
kind: Config
clusters:
- cluster: {{server: https://gpu.example:6443, {tls}}}
  name: c
contexts:
- context: {{cluster: c, user: u, namespace: gpu-test}}
  name: ctx
current-context: ctx
users:
- name: u
  user: {{token: t}}
"""


def _cluster(ca, *, kind="llmservice", endpoint="dns", in_cluster=False):
    return SimpleNamespace(
        k8s_in_cluster=in_cluster, kubeconfig_content=_kubeconfig(ca), k8s_kubeconfig="",
        k8s_context="", k8s_workload_kind=kind, k8s_endpoint=endpoint,
    )


@pytest.fixture
def own_ca(tmp_path, monkeypatch):
    path = tmp_path / "ca.crt"
    path.write_bytes(OWN)
    monkeypatch.setattr(locality, "OWN_CA", path)
    return path


def test_the_same_ca_is_the_same_cluster(own_ca):
    assert same_cluster(_cluster(OWN), own_ca) is True
    assert same_cluster(_cluster(OTHER), own_ca) is False


def test_a_rotating_bundle_that_shares_a_ca_is_the_same_cluster(own_ca):
    assert same_cluster(_cluster(OTHER + OWN), own_ca) is True


def test_unknown_without_a_ca_on_either_side(own_ca, tmp_path):
    assert same_cluster(_cluster(None), own_ca) is None  # insecure kubeconfig
    assert same_cluster(_cluster(OTHER), tmp_path / "missing.crt") is None  # not in a pod


def test_in_cluster_access_is_the_platforms_own_cluster(tmp_path):
    assert same_cluster(_cluster(None, in_cluster=True), tmp_path / "missing.crt") is True


def test_only_service_names_on_another_cluster_are_unreachable(own_ca):
    assert unreachable_by_name(_cluster(OTHER), own_ca)
    assert not unreachable_by_name(_cluster(OTHER, endpoint="nodeport"), own_ca)
    assert not unreachable_by_name(_cluster(OWN), own_ca)
    assert not unreachable_by_name(_cluster(None), own_ca)  # unknown: not refused
    assert not unreachable_by_name(_cluster(OTHER, kind="deployment"), own_ca)


async def test_another_cluster_by_service_name_is_refused_at_save(client, own_ca):  # noqa: F811
    response = await client.post("/api/clusters", json={
        "name": "b300", "kubeconfig": _kubeconfig(OTHER), "namespace": "runs",
        "workload_kind": "llmservice", "endpoint": "dns",
    })
    assert response.status_code == 422
    assert "NodePort" in response.json()["detail"]

    created = await _add(client, "b300", kubeconfig=_kubeconfig(OTHER),
                         workload_kind="llmservice", endpoint="nodeport")
    assert created["same_cluster"] is False
    # Switching it back to Service names is refused too, and changes nothing.
    edit = {**created, "kubeconfig": "", "endpoint": "dns"}
    response = await client.put(f"/api/clusters/{created['id']}", json=edit)
    assert response.status_code == 422
    listed = (await client.get("/api/clusters")).json()
    assert [c["endpoint"] for c in listed] == ["nodeport"]


async def test_the_platforms_own_cluster_keeps_service_names(client, own_ca):  # noqa: F811
    created = await _add(client, "local", kubeconfig=_kubeconfig(OWN),
                         workload_kind="llmservice", endpoint="dns")
    assert created["endpoint"] == "dns" and created["same_cluster"] is True


async def test_the_form_can_ask_before_saving(client, own_ca):  # noqa: F811
    ask = lambda ca: client.post("/api/clusters/locality", json={"kubeconfig": _kubeconfig(ca)})  # noqa: E731
    assert (await ask(OWN)).json() == {"same_cluster": True}
    assert (await ask(OTHER)).json() == {"same_cluster": False}
    assert (await ask(None)).json() == {"same_cluster": None}
    assert (await client.post("/api/clusters/locality", json={"in_cluster": True})).json() == {
        "same_cluster": True}
