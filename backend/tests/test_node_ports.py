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

from app.control.launch.base import LaunchDeferred, WaitingFor
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
    over.setdefault("candidate_id", 1)
    s.add(Run(id=rid, campaign_id=1, machine_id=machine, status=status, **over))


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


def test_a_run_holds_the_port_from_placement_until_its_teardown_is_confirmed():
    """Handed out with the cards: a placed run that has not launched holds it
    too, so the scheduler never places a second run for the one port."""
    factory = _db()
    with factory() as s:
        _cluster(s, 1)
        _cluster(s, 2)
        _run(s, 11, machine=1, status=RunStatus.SUCCEEDED.value)
        _run(s, 12, machine=1, kind=RunKind.EXTERNAL.value)  # launched by nobody here
        s.commit()
        assert port_holder(s, s.get(Machine, 2)) is None
        s.get(Run, 11).teardown_pending = True
        s.commit()
        assert port_holder(s, s.get(Machine, 2)).id == 11
        s.get(Run, 11).teardown_pending = False
        _run(s, 13, machine=1, status=RunStatus.PENDING.value)
        s.commit()
        assert port_holder(s, s.get(Machine, 2)).id == 13


def test_pending_runs_get_the_port_in_the_order_they_were_placed():
    """Runs made outside the queue (a policy's launches) each wait only for
    the runs placed before them, so neither blocks the other forever."""
    factory = _db()
    with factory() as s:
        _cluster(s, 1)
        _run(s, 20, machine=1, status=RunStatus.PENDING.value)
        _run(s, 21, machine=1, status=RunStatus.PENDING.value)
        s.commit()
        machine = s.get(Machine, 1)
        assert port_busy_reason(s, machine, exclude_run=20) is None
        assert "goes to run 20" in port_busy_reason(s, machine, exclude_run=21)
        s.get(Run, 20).status = RunStatus.LAUNCHING.value
        s.commit()
        assert "held by run 20" in port_busy_reason(s, machine, exclude_run=21)
        assert port_busy_reason(s, machine, exclude_run=20) is None


def test_an_admission_on_another_registration_holds_the_port():
    """A campaign admitted to a machine before its run is placed holds the
    port as that run will: a second registration of the cluster may not take
    it in between. Its own admission does not count against itself."""
    from app import plugins
    from app.control.orchestrator.node_ports import port_siblings
    from app.control.orchestrator.occupancy import Reservation

    factory = _db()
    with factory() as s:
        _cluster(s, 1)
        _cluster(s, 2, server="https://other:6443")  # same node host
        _cluster(s, 3, host="elsewhere", server="https://elsewhere:6443")
        s.commit()
        assert port_siblings(s, {1}) == {1, 2}

        class Holding:
            name, reservations = "t", None

        held = Holding()
        held.reservations = lambda session, machine: (
            [Reservation(campaign_id=1, label="submission #5", cards=1, share=False)]
            if machine.id == 1 else []
        )
        original = plugins.enabled
        plugins.enabled = lambda: [held]
        try:
            assert "held for submission #5" in port_busy_reason(s, s.get(Machine, 2))
            assert port_busy_reason(s, s.get(Machine, 2), campaign_id=1) is None
            assert port_busy_reason(s, s.get(Machine, 3)) is None
        finally:
            plugins.enabled = original


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
    with pytest.raises(LaunchDeferred, match="node port 31080 .* held by a Service") as exc:
        driver.launch(_spec())
    assert exc.value.waiting_for == WaitingFor.NODE_PORT
    # The Service goes first, so the refusal comes before the Deployment.
    assert api.applied == []
    assert ("deployment", "autotune-run-7") in api.deleted


def test_an_llmservice_claims_the_port_first_then_hands_it_to_its_engine():
    """The port first: refused, no LLMService is made (and withdrawn again on
    every tick of the wait). Taken, it is handed to the LLMService, so it goes
    with the run however the run ends, not only through the driver's own
    teardown."""
    from app.control.launch.llmservice import LLMServiceDriver
    from tests.test_llmservice_driver import LlmsvcApi, _settings, _spec

    settings = _settings(k8s_endpoint="nodeport", k8s_node_host="10.1.1.1",
                         k8s_service_nodeport=31080)

    class WithUid(LlmsvcApi):
        def apply(self, manifest):
            super().apply(manifest)
            if manifest.get("kind") == "LLMService":
                manifest.setdefault("metadata", {})["uid"] = "u"

    api = _refusing(WithUid, settings)
    with pytest.raises(LaunchDeferred, match="node port 31080") as exc:
        LLMServiceDriver(api=api, settings=settings).launch_nodes(_spec())
    assert exc.value.waiting_for == WaitingFor.NODE_PORT
    assert api.applied == [], "nothing is applied while the port is held"

    api = WithUid(settings)
    driver = LLMServiceDriver(api=api, settings=settings)
    driver.launch_nodes(_spec())
    claim, engine, owned = api.applied[:3]
    assert claim["metadata"]["name"] == "autotune-run-7-np" and engine["kind"] == "LLMService"
    assert claim["spec"]["ports"][0]["nodePort"] == 31080
    assert not claim["metadata"]["ownerReferences"]
    assert owned["metadata"]["ownerReferences"][0]["uid"] == "u"
    assert "app" not in owned["spec"]["selector"], "selects nothing until the front door exists"
    # Once the chart's Service is there, the claim is pointed at what it selects.
    api.objects[("service", "autotune-run-7")] = {"spec": {
        "selector": {"app": "front"}, "ports": [{"port": 30000}]}}
    api.objects[("service", "autotune-run-7-np")]["spec"]["clusterIP"] = "10.96.0.9"
    assert driver.attach(_spec()).endpoint_url == "http://10.1.1.1:31080"
    moved = api.applied[-1]
    assert moved["spec"]["selector"] == {"app": "front"}
    assert moved["spec"]["clusterIP"] == "10.96.0.9"
    assert moved["metadata"]["ownerReferences"][0]["uid"] == "u"


def test_a_port_service_that_will_not_update_does_not_keep_a_run_from_teardown():
    """attach() is how a teardown finds the run: raising there left the
    engine to the janitor, whose path never deleted the port Service."""
    from app.control.launch.llmservice import LLMServiceDriver
    from tests.test_llmservice_driver import LlmsvcApi, _settings, _spec

    settings = _settings(k8s_endpoint="nodeport", k8s_node_host="10.1.1.1",
                         k8s_service_nodeport=31080)
    api = LlmsvcApi(settings)
    driver = LLMServiceDriver(api=api, settings=settings)
    driver.launch_nodes(_spec())
    api.objects[("service", "autotune-run-7")] = {"spec": {
        "selector": {"app": "front"}, "ports": [{"port": 30000}]}}

    def refuse(manifest):
        raise K8sError("client apply Service/autotune-run-7-np failed: Unprocessable Entity")

    api.apply = refuse
    handle = driver.attach(_spec())
    assert handle is not None and handle.endpoint_url == ""
    driver.teardown(handle)
    assert (LLMSVC_RESOURCE, "autotune-run-7") in api.deleted


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
            raise LaunchDeferred("held elsewhere", WaitingFor.NODE_PORT)
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
        run = s.get(Run, mine.id)
        assert run.waiting_for == "node_port" and "held by run 50" in run.waiting_reason
    assert supervisor.driver.launched == []
    with factory() as s:
        s.get(Run, 50).status = RunStatus.SUCCEEDED.value
        s.commit()
    supervisor.tick()
    with factory() as s:
        run = s.get(Run, mine.id)
        assert run.status == RunStatus.LAUNCHING.value
        assert (run.waiting_for, run.waiting_reason, run.waiting_since) == ("", "", None)


# -- a port that outlives its run ---------------------------------------------


def _llmservice_on_fixed_port():
    from app.control.launch.llmservice import LLMServiceDriver
    from tests.test_llmservice_driver import LlmsvcApi, _settings

    settings = _settings(k8s_endpoint="nodeport", k8s_node_host="10.1.1.1",
                         k8s_service_nodeport=31080)

    class Api(LlmsvcApi):
        def apply(self, manifest):
            super().apply(manifest)
            if manifest.get("kind") == "LLMService":
                manifest.setdefault("metadata", {})["uid"] = "u"

        def list(self, resource, label_selector=""):
            if resource == "service":
                return [o for (r, _), o in self.objects.items() if r == "service"]
            return super().list(resource, label_selector)

    api = Api(settings)
    return api, LLMServiceDriver(api=api, settings=settings)


def test_a_teardown_never_claims_the_port_again():
    """attach() runs on every tick of a teardown. While the LLMService waited
    on its finalizer, the claim teardown had just deleted was made again
    here, and held the cluster's port after the run was over."""
    from tests.test_llmservice_driver import _spec

    api, driver = _llmservice_on_fixed_port()
    driver.launch_nodes(_spec())
    api.objects[("service", "autotune-run-7")] = {"spec": {
        "selector": {"app": "front"}, "ports": [{"port": 30000}]}}
    driver.teardown(driver.attach(_spec()))
    # The operator is still uninstalling: the LLMService and its front door remain.
    api.objects[(LLMSVC_RESOURCE, "autotune-run-7")] = {"metadata": {
        "uid": "u", "deletionTimestamp": "2026-11-04T06:53:54Z"}}
    api.objects[("service", "autotune-run-7")] = {"spec": {
        "selector": {"app": "front"}, "ports": [{"port": 30000}]}}
    handle = driver.attach(_spec())
    assert ("service", "autotune-run-7-np") not in api.objects
    assert handle.endpoint_url == ""
    # A claim still there while the engine goes is not pointed anywhere new.
    api.objects[("service", "autotune-run-7-np")] = {"spec": {"selector": {"x": "y"}}}
    applied = len(api.applied)
    driver.attach(_spec())
    assert len(api.applied) == applied


def test_a_run_is_not_gone_while_its_port_service_is_there():
    from tests.test_llmservice_driver import _spec

    api, driver = _llmservice_on_fixed_port()
    driver.launch_nodes(_spec())
    handle = driver.attach(_spec())
    del api.objects[(LLMSVC_RESOURCE, "autotune-run-7")]
    assert not driver.is_gone(handle), "the claim still holds the port"
    assert driver.release_leftovers(_spec())
    assert ("service", "autotune-run-7-np") not in api.objects
    assert driver.is_gone(handle)


def test_a_refused_port_names_the_service_holding_it():
    from tests.test_llmservice_driver import _spec

    api, driver = _llmservice_on_fixed_port()
    api.objects[("service", "autotune-run-6-np")] = {
        "metadata": {"name": "autotune-run-6-np"},
        "spec": {"ports": [{"nodePort": 31080}]},
    }
    real = api.apply

    def apply(manifest):
        if manifest.get("kind") == "Service":
            raise K8sError(REFUSED)
        real(manifest)

    api.apply = apply
    with pytest.raises(LaunchDeferred, match="held by Service runs/autotune-run-6-np"):
        driver.launch_nodes(_spec())


def test_the_janitor_holds_the_port_until_what_the_run_left_is_gone():
    from tests.test_supervisor import make_supervisor

    supervisor, factory = make_supervisor()
    _on_a_fixed_port_cluster(factory)
    supervisor._driver_for = lambda machine: supervisor.driver
    left = {"n": 2}

    def release_leftovers(spec):
        left["n"] -= 1
        return left["n"] <= 0

    supervisor.driver.release_leftovers = release_leftovers
    supervisor.driver.attach = lambda spec: None
    with factory() as s:
        s.add(Candidate(id=60, campaign_id=1, config={}, config_hash="h60"))
        _run(s, 60, machine=1, status=RunStatus.FAILED.value, teardown_pending=True,
             candidate_id=60)
        s.commit()
    with factory() as s:
        supervisor._reap_teardowns(s)
        s.commit()
        assert s.get(Run, 60).teardown_pending, "its port Service is still there"
        assert port_holder(s, s.get(Machine, 1)).id == 60
        supervisor._reap_teardowns(s)
        s.commit()
        assert not s.get(Run, 60).teardown_pending


def test_runs_waiting_for_the_port_get_it_in_order():
    """The first run the cluster refused the port to is first in line: a later
    waiter, and a new placement, wait behind it."""
    factory = _db()
    with factory() as s:
        _cluster(s, 1)
        _run(s, 10, machine=1, status=RunStatus.PENDING.value,
             waiting_for="node_port", waiting_reason="held")
        _run(s, 11, machine=1, status=RunStatus.PENDING.value,
             waiting_for="node_port", waiting_reason="held")
        _run(s, 12, machine=1, status=RunStatus.PENDING.value)  # not refused yet
        s.commit()
        machine = s.get(Machine, 1)
        assert port_holder(s, machine, exclude_run=10) is None
        assert port_holder(s, machine, exclude_run=11).id == 10
        assert "goes to run 10" in port_busy_reason(s, machine, exclude_run=11)
        assert port_holder(s, machine).id == 10, "no new run is placed behind them"


def test_an_engine_that_will_not_apply_takes_its_claim_with_it():
    from tests.test_llmservice_driver import _spec

    api, driver = _llmservice_on_fixed_port()
    real = api.apply

    def apply(manifest):
        if manifest.get("kind") == "LLMService":
            raise K8sError("client apply LLMService/autotune-run-7 failed: Forbidden")
        real(manifest)

    api.apply = apply
    with pytest.raises(K8sError):
        driver.launch_nodes(_spec())
    assert ("service", "autotune-run-7-np") not in api.objects


def test_a_claim_from_an_attempt_that_stopped_short_is_kept_as_it_is():
    """A worker that died between the claim and the engine leaves the claim;
    the next attempt uses it rather than replacing it (a Service replaced
    without its address is refused)."""
    from tests.test_llmservice_driver import _spec

    api, driver = _llmservice_on_fixed_port()
    api.objects[("service", "autotune-run-7-np")] = {
        "metadata": {"name": "autotune-run-7-np"},
        "spec": {"clusterIP": "10.96.0.9", "selector": {}, "ports": [{"nodePort": 31080}]},
    }
    driver.launch_nodes(_spec())
    kinds = [m["kind"] for m in api.applied]
    assert kinds == ["LLMService", "Service"], "the engine, then the claim handed to it"
    owned = api.applied[-1]
    assert owned["spec"]["clusterIP"] == "10.96.0.9"
    assert owned["metadata"]["ownerReferences"][0]["uid"] == "u"


def test_a_refusal_names_the_autotune_run_that_left_the_service():
    from tests.test_llmservice_driver import _spec

    api, driver = _llmservice_on_fixed_port()
    api.objects[("service", "autotune-run-6-np")] = {
        "metadata": {"name": "autotune-run-6-np", "labels": {
            "autotune.modelsphere.dev/managed": "true", "autotune.modelsphere.dev/run": "6"}},
        "spec": {"ports": [{"nodePort": 31080}]},
    }
    real = api.apply

    def apply(manifest):
        if manifest.get("kind") == "Service":
            raise K8sError(REFUSED)
        real(manifest)

    api.apply = apply
    with pytest.raises(LaunchDeferred) as exc:
        driver.launch_nodes(_spec())
    assert (exc.value.holder_service, exc.value.holder_run) == ("autotune-run-6-np", 6)

    assert not driver.release_orphan_port("autotune-run-6-np", 5), "labels for another run"
    assert ("service", "autotune-run-6-np") in api.objects
    api.objects[("service", "autotune-run-6-np")]["metadata"]["labels"].pop(
        "autotune.modelsphere.dev/managed")
    assert not driver.release_orphan_port("autotune-run-6-np", 6), "not ours any more"
    api.objects[("service", "autotune-run-6-np")]["metadata"]["labels"][
        "autotune.modelsphere.dev/managed"] = "true"
    assert driver.release_orphan_port("autotune-run-6-np", 6)
    assert ("service", "autotune-run-6-np") not in api.objects


def test_a_deployment_runs_service_left_behind_holds_the_port_until_it_is_gone():
    from app.control.launch.k8s import K8sDriver
    from tests.test_k8s_driver import FakeK8sApi, _clone, _spec

    settings = _clone(k8s_workload_kind="deployment", k8s_node_host="198.51.100.84",
                      k8s_service_nodeport=31080)
    api = FakeK8sApi(settings)
    driver = K8sDriver(api=api, settings=settings)
    driver.launch(_spec())
    api.objects.pop(("deployment", "autotune-run-7"))
    assert ("service", "autotune-run-7") in api.objects
    assert driver.release_leftovers(_spec())
    assert ("service", "autotune-run-7") not in api.objects
    # Without a fixed port nothing outlives the Deployment that matters.
    plain = K8sDriver(api=FakeK8sApi(_clone(k8s_workload_kind="deployment")),
                      settings=_clone(k8s_workload_kind="deployment"))
    assert plain.release_leftovers(_spec())


def test_a_teardown_that_cannot_remove_the_port_service_is_confirmed_at_giveup():
    """A cluster that will not delete it must not hold the cards forever: at
    the give-up the teardown is confirmed, and the next launch finds the
    Service, named, and releases it as left over."""
    from tests.test_supervisor import make_supervisor

    supervisor, factory = make_supervisor()
    _on_a_fixed_port_cluster(factory)
    supervisor._driver_for = lambda machine: supervisor.driver
    supervisor.driver.release_leftovers = lambda spec: False
    supervisor.driver.attach = lambda spec: None
    with factory() as s:
        s.add(Candidate(id=61, campaign_id=1, config={}, config_hash="h61"))
        _run(s, 61, machine=1, status=RunStatus.FAILED.value, teardown_pending=True,
             candidate_id=61)
        s.commit()
    with factory() as s:
        supervisor._reap_teardowns(s)
        s.commit()
        assert s.get(Run, 61).teardown_pending
        from datetime import timedelta

        from app.control.orchestrator.lifecycle import now

        s.get(Run, 61).finished_at = now() - timedelta(
            seconds=supervisor.settings.teardown_giveup_seconds + 1)
        s.commit()
        supervisor._reap_teardowns(s)
        s.commit()
        assert not s.get(Run, 61).teardown_pending
