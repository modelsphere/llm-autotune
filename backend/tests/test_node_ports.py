"""A cluster's fixed NodePort is one port for every run on it.

A firewall that lets one port through (`service_nodeport`) puts each run's
endpoint on that port, and a NodePort belongs to the whole cluster: a second
run's Service is refused ("provided port is already allocated") however many
cards are free. The same cluster registered twice names the port twice. Runs
now wait for the port the way they wait for cards, and a refusal from outside
the platform defers the launch instead of failing it."""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.control.launch.base import LaunchDeferred
from app.control.launch.failures import classify_failure, is_infrastructure
from app.control.launch.k8s_api import LLMSVC_RESOURCE, K8sError
from app.control.orchestrator.node_ports import port_busy_reason, port_holder
from app.core.secrets import encrypt_secret
from app.db.base import Base
from app.db.models import (
    Campaign,
    CampaignStatus,
    Candidate,
    Cluster,
    Event,
    Machine,
    MachineState,
    Run,
    RunKind,
    RunStatus,
    User,
)

REFUSED = (
    'client apply Service/autotune-run-7 failed: Unprocessable Entity: Service '
    '"autotune-run-7" is invalid: spec.ports[0].nodePort: Invalid value: 31080: '
    "provided port is already allocated"
)


def _kubeconfig(server: str) -> str:
    return (
        "apiVersion: v1\ncurrent-context: c\n"
        f"clusters:\n- name: k\n  cluster:\n    server: {server}\n"
        "contexts:\n- name: c\n  context:\n    cluster: k\n    user: u\n"
        "users:\n- name: u\n  user:\n    token: t\n"
    )


def _db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as s:
        s.add(User(id=1, username="u", password_hash="x"))
        s.add(Campaign(id=1, owner_id=1, name="c", engine="sglang", image="img",
                       model_path="/models/m", served_model_name="m", search_space={},
                       status=CampaignStatus.ACTIVE.value))
        s.add(Candidate(id=1, campaign_id=1, config={}, config_hash="h1"))
        s.commit()
    return factory


def _cluster(s, cid, *, host="198.51.100.84", port=31080, kind="deployment",
             endpoint="dns", server="https://10.0.0.1:6443"):
    s.add(Cluster(id=cid, name=f"c{cid}", workload_kind=kind, endpoint=endpoint,
                  node_host=host, service_nodeport=port, namespace="runs", api_mode="client",
                  kubeconfig_enc=encrypt_secret(_kubeconfig(server))))
    s.add(Machine(id=cid, name=f"m{cid}", host="", driver="k8s", cluster_id=cid,
                  gpu_count=8, state=MachineState.AVAILABLE.value))


def _run(s, rid, machine, status=RunStatus.BENCHING.value, **over):
    s.add(Run(id=rid, campaign_id=1, candidate_id=1, machine_id=machine, status=status, **over))


# -- who holds the port --------------------------------------------------------


def test_two_registrations_of_one_cluster_share_its_port():
    """One cluster added twice, once per launch mode: one port, two rows."""
    factory = _db()
    with factory() as s:
        _cluster(s, 1, kind="llmservice", endpoint="nodeport")
        _cluster(s, 2, server="https://other:6443")  # same node host
        _run(s, 10, machine=1)
        s.commit()
        assert port_holder(s, s.get(Machine, 2)).id == 10
        assert "node port 31080 is held by run 10" in port_busy_reason(s, s.get(Machine, 2))
        # The holder itself, and a second run on its own machine.
        assert port_holder(s, s.get(Machine, 1), exclude_run=10) is None
        assert port_holder(s, s.get(Machine, 1)).id == 10


def test_the_same_api_server_is_the_same_cluster_whatever_the_host():
    factory = _db()
    with factory() as s:
        _cluster(s, 1, host="a")
        _cluster(s, 2, host="b")
        _run(s, 10, machine=1)
        s.commit()
        assert port_holder(s, s.get(Machine, 2)).id == 10


def test_a_different_port_or_cluster_or_no_fixed_port_is_free():
    factory = _db()
    with factory() as s:
        _cluster(s, 1)
        _cluster(s, 2, port=30028)
        _cluster(s, 3, host="elsewhere", server="https://elsewhere:6443")
        _cluster(s, 4, port=0)
        # Reached by DNS: an llmservice cluster never asks for its port.
        _cluster(s, 5, kind="llmservice", endpoint="dns")
        _run(s, 10, machine=1)
        s.commit()
        for mid in (2, 3, 4, 5):
            assert port_holder(s, s.get(Machine, mid)) is None, mid


def test_only_a_launched_run_or_one_still_tearing_down_holds_it():
    factory = _db()
    with factory() as s:
        _cluster(s, 1)
        _cluster(s, 2)
        _run(s, 10, machine=1, status=RunStatus.PENDING.value)  # waiting: holds nothing
        _run(s, 11, machine=1, status=RunStatus.SUCCEEDED.value)
        _run(s, 12, machine=1, kind=RunKind.EXTERNAL.value)  # launched by nobody here
        s.commit()
        assert port_holder(s, s.get(Machine, 2)) is None
        s.get(Run, 11).teardown_pending = True
        s.commit()
        assert port_holder(s, s.get(Machine, 2)).id == 11


# -- the drivers: a refusal defers ---------------------------------------------


def test_k8s_words_a_taken_node_port_as_a_port_conflict():
    assert classify_failure(REFUSED) == "port_conflict"
    assert is_infrastructure("port_conflict")


def _refusing(api_cls, settings):
    class Refusing(api_cls):
        def apply(self, manifest):
            items = manifest if isinstance(manifest, list) else [manifest]
            for m in items:
                if m.get("kind") == "Service" and (m["spec"].get("ports") or [{}])[0].get(
                    "nodePort"
                ):
                    raise K8sError(REFUSED)
                super().apply(m)

    return Refusing(settings)


def test_a_deployment_waits_for_a_port_held_elsewhere_with_nothing_left_running():
    from app.control.launch.k8s import K8sDriver
    from tests.test_k8s_driver import FakeK8sApi, _clone, _spec

    settings = _clone(k8s_workload_kind="deployment", k8s_node_host="198.51.100.84",
                      k8s_service_nodeport=31080)
    api = _refusing(FakeK8sApi, settings)
    driver = K8sDriver(api=api, settings=settings)
    with pytest.raises(LaunchDeferred, match="node port 31080 .* held by another Service"):
        driver.launch(_spec())
    # The Service goes first, so the refusal comes before the Deployment.
    assert api.applied == []
    assert ("deployment", "autotune-run-7") in api.deleted


def test_an_llmservice_claims_the_port_before_its_engine_is_applied():
    from app.control.launch.llmservice import LLMServiceDriver
    from tests.test_llmservice_driver import LlmsvcApi, _settings, _spec

    settings = _settings(k8s_endpoint="nodeport", k8s_node_host="10.1.1.1",
                         k8s_service_nodeport=31080)
    api = _refusing(LlmsvcApi, settings)
    with pytest.raises(LaunchDeferred, match="node port 31080"):
        LLMServiceDriver(api=api, settings=settings).launch_nodes(_spec())
    assert not any(m["kind"] == "LLMService" for m in api.applied)

    api = LlmsvcApi(settings)
    driver = LLMServiceDriver(api=api, settings=settings)
    driver.launch_nodes(_spec())
    claim, engine = api.applied[:2]
    assert claim["metadata"]["name"] == "autotune-run-7-np" and engine["kind"] == "LLMService"
    assert claim["spec"]["ports"][0]["nodePort"] == 31080
    assert "app" not in claim["spec"]["selector"], "selects nothing until the front door exists"
    # Once the chart's Service is there, the claim is pointed at what it selects.
    api.objects[(LLMSVC_RESOURCE, "autotune-run-7")] = {"metadata": {"uid": "u"}}
    api.objects[("service", "autotune-run-7")] = {"spec": {
        "selector": {"app": "front"}, "ports": [{"port": 30000}]}}
    api.objects[("service", "autotune-run-7-np")]["spec"]["clusterIP"] = "10.96.0.9"
    assert driver.attach(_spec()).endpoint_url == "http://10.1.1.1:31080"
    moved = api.applied[-1]
    assert moved["spec"]["selector"] == {"app": "front"}
    assert moved["spec"]["clusterIP"] == "10.96.0.9"
    assert moved["metadata"]["ownerReferences"][0]["uid"] == "u"


# -- the supervisor: a run waits for the port as for cards ----------------------


def _on_a_fixed_port_cluster(factory):
    with factory() as s:
        _cluster(s, 7)
        _cluster(s, 8, kind="llmservice", endpoint="nodeport")
        m = s.get(Machine, 1)
        m.driver, m.cluster_id = "k8s", 7
        s.commit()


def test_a_run_waits_while_another_registration_holds_the_port():
    from tests.test_supervisor import make_supervisor

    supervisor, factory = make_supervisor()
    _on_a_fixed_port_cluster(factory)
    supervisor._driver_for = lambda machine: supervisor.driver
    with factory() as s:
        s.add(Campaign(id=2, owner_id=1, name="other", engine="sglang", image="img",
                       model_path="/models/m", served_model_name="m", search_space={},
                       status=CampaignStatus.DONE.value))
        s.add(Candidate(id=50, campaign_id=2, config={}, config_hash="h50"))
        s.add(Run(id=50, campaign_id=2, candidate_id=50, machine_id=8,
                  status=RunStatus.BENCHING.value))
        s.commit()

    supervisor.tick()
    with factory() as s:
        assert s.scalars(select(Run).where(Run.campaign_id == 1)).all() == []
    assert supervisor.driver.launched == []

    with factory() as s:
        s.get(Run, 50).status = RunStatus.SUCCEEDED.value
        s.commit()
    supervisor.tick()
    with factory() as s:
        run = s.scalars(select(Run).where(Run.campaign_id == 1)).one()
        assert run.status == RunStatus.LAUNCHING.value


def test_a_placed_run_whose_port_is_taken_stays_pending_and_says_why():
    from tests.test_supervisor import make_supervisor

    supervisor, factory = make_supervisor()
    _on_a_fixed_port_cluster(factory)
    supervisor._driver_for = lambda machine: supervisor.driver
    real_launch = supervisor.driver.launch
    deferrals = {"left": 1}

    def launch(spec):
        if deferrals["left"]:
            deferrals["left"] -= 1
            raise LaunchDeferred("held elsewhere")
        return real_launch(spec)

    supervisor.driver.launch = launch
    supervisor.tick()  # placed, deferred once: pending
    with factory() as s:
        mine = s.scalars(select(Run)).one()
        s.add(Campaign(id=2, owner_id=1, name="other", engine="sglang", image="img",
                       model_path="/models/m", served_model_name="m", search_space={},
                       status=CampaignStatus.DONE.value))
        s.add(Candidate(id=50, campaign_id=2, config={}, config_hash="h50"))
        s.add(Run(id=50, campaign_id=2, candidate_id=50, machine_id=8,
                  status=RunStatus.BENCHING.value))
        s.commit()
    supervisor.tick()
    with factory() as s:
        assert s.get(Run, mine.id).status == RunStatus.PENDING.value
        reasons = [e.payload.get("reason") for e in s.scalars(select(Event)).all()
                   if e.kind == "run_waiting_for_cluster"]
        assert reasons == ["held elsewhere"]  # one event per edge, not per tick
    assert supervisor.driver.launched == []
    with factory() as s:
        s.get(Run, 50).status = RunStatus.SUCCEEDED.value
        s.commit()
    supervisor.tick()
    with factory() as s:
        assert s.get(Run, mine.id).status == RunStatus.LAUNCHING.value
