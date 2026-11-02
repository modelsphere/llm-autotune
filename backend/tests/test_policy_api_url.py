"""Which address a policy calls home on: the API's Service name only on
AutoTune's own cluster, `public_api_url` everywhere else.

Regression: with gpuCluster.inCluster the chart set the public address to the
Service name, so a policy on a cluster added later was handed a name that does
not resolve there.
"""

from types import SimpleNamespace

import pytest

from app.control.launch import locality
from app.control.orchestrator.policy_session import api_url_for

INTERNAL = "http://llm-autotune-api.llm-autotune.svc.cluster.local:8000"
PUBLIC = "https://autotune.example.com"


def _settings(internal=INTERNAL, public=PUBLIC):
    return SimpleNamespace(internal_api_url=internal, public_api_url=public)


def _machine(driver="k8s", cluster_id=7):
    return SimpleNamespace(driver=driver, cluster_id=cluster_id)


@pytest.fixture
def where(monkeypatch):
    """Stand-in for the CA comparison: cluster id -> same_cluster's answer."""
    answers: dict = {}
    monkeypatch.setattr(
        "app.control.launch.clusters.resolve_cluster", lambda cid: SimpleNamespace(cid=cid)
    )
    monkeypatch.setattr(locality, "same_cluster", lambda c, own_ca=None: answers.get(c.cid))
    return answers


def test_own_cluster_gets_the_service_name(where):
    where[7] = True
    assert api_url_for(_settings(), _machine()) == INTERNAL


def test_another_cluster_gets_the_public_address(where):
    where[7] = False
    assert api_url_for(_settings(), _machine()) == PUBLIC


def test_a_cluster_of_unknown_locality_gets_the_public_address(where):
    where[7] = None
    assert api_url_for(_settings(), _machine()) == PUBLIC


def test_an_ssh_machine_gets_the_public_address(where):
    where[7] = True
    assert api_url_for(_settings(), _machine(driver="ssh_docker")) == PUBLIC


def test_no_public_address_leaves_another_cluster_with_none(where):
    where[7] = False
    assert api_url_for(_settings(public=""), _machine()) == ""
    where[7] = True
    assert api_url_for(_settings(public=""), _machine()) == INTERNAL
