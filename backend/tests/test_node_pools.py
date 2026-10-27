"""A k8s machine can be a pool of nodes: a GPU type rather than one hostname.

The cluster decides where each pod goes and whether it waits; the platform
only budgets how many cards a session uses at once, refuses what no node could
ever hold, and says plainly when a run is waiting for cards rather than
starting. These pin that division of labour.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.control.launch.base import DeploymentState, MachineInfo
from app.db.models import (
    Campaign,
    CampaignStatus,
    Candidate,
    ContenderStatus,
    Event,
    Machine,
    Policy,
    PolicyContender,
    PolicySession,
    PolicySessionStatus,
    Run,
    RunKind,
    RunStatus,
)
from app.hardware import is_node_pool
from tests.test_k8s_driver import FakeK8sApi, _clone, _driver
from tests.test_lease_api import client  # noqa: F401
from tests.test_policy_sessions import SPACE, _boot, _session_row, night  # noqa: F401
from tests.test_supervisor import FakeDriver, PassEvaluator, make_supervisor

POOL = "nvidia.com/gpu.product=NVIDIA-H800"


# -- what a pool is -------------------------------------------------------------


def test_a_pool_is_a_k8s_machine_not_pinned_to_one_host():
    assert is_node_pool(MachineInfo(name="p", host="", driver="k8s", node_selector=POOL))
    assert is_node_pool(MachineInfo(name="p", host="", driver="k8s", node_selector=""))
    assert not is_node_pool(MachineInfo(
        name="n", host="", driver="k8s",
        node_selector=f"kubernetes.io/hostname=gpu-b,{POOL}",
    ))
    assert not is_node_pool(MachineInfo(name="b", host="10.0.0.1", driver="ssh_docker"))


# -- free cards, read from the cluster --------------------------------------------


def _node(name: str, gpus: int, *, ready: bool = True, cordoned: bool = False) -> dict:
    return {
        "metadata": {"name": name, "labels": {"nvidia.com/gpu.product": "NVIDIA-H800"}},
        "spec": {"unschedulable": True} if cordoned else {},
        "status": {
            "allocatable": {"nvidia.com/gpu": str(gpus)},
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
        },
    }


def _pod(node: str, gpus: int) -> dict:
    return {"spec": {"nodeName": node, "containers": [
        {"resources": {"limits": {"nvidia.com/gpu": str(gpus)}}}]}}


class _ClusterApi(FakeK8sApi):
    cluster_pods: list[dict] | None = None

    def list_cluster_pods(self):
        if self.cluster_pods is None:
            raise RuntimeError("forbidden")
        return list(self.cluster_pods)


def test_the_probe_counts_free_cards_on_nodes_that_take_pods():
    settings = _clone(k8s_api_mode="kubectl")
    api = _ClusterApi(settings)
    api.nodes = [_node("gpu-a", 8), _node("gpu-b", 8), _node("gpu-c", 7, cordoned=True)]
    # Other teams' pods, in any namespace: 8 on gpu-a, 3 on gpu-b.
    api.cluster_pods = [_pod("gpu-a", 8), _pod("gpu-b", 2), _pod("gpu-b", 1)]
    probe = _driver(settings, api).probe_capacity(
        MachineInfo(name="pool", host="", driver="k8s", node_selector=POOL)
    )
    assert probe["gpu_count"] == 23  # installed, every matching node
    assert probe["gpu_free"] == 5    # 0 on gpu-a, 5 on gpu-b, none on the cordoned gpu-c
    assert probe["node_gpu_max"] == 8
    assert probe["node_count"] == 3
    assert probe["gpu_types"] == ["H800"]

    # Without a cluster-wide read of pods, free is unknown, not zero.
    api.cluster_pods = None
    probe = _driver(settings, api).probe_capacity(
        MachineInfo(name="pool", host="", driver="k8s", node_selector=POOL)
    )
    assert probe["gpu_free"] is None
    assert any("free cards are unknown" in w for w in probe["warnings"])


def test_a_node_cordoned_for_us_counts_when_our_pods_tolerate_the_cordon():
    """An admin cordons GPU nodes to keep everything else off them, for this
    platform to use. With the cordon's toleration on the cluster, our pods
    schedule there, so the node's cards are free cards, not none."""
    settings = _clone(k8s_api_mode="kubectl",
                      k8s_tolerations="node.kubernetes.io/unschedulable:NoSchedule")
    api = _ClusterApi(settings)
    api.nodes = [_node("gpu-a", 8, cordoned=True), _node("gpu-b", 8, cordoned=True)]
    api.cluster_pods = [_pod("gpu-b", 2)]
    probe = _driver(settings, api).probe_capacity(
        MachineInfo(name="pool", host="", driver="k8s", node_selector=POOL)
    )
    assert probe["gpu_free"] == 14 and probe["node_gpu_max"] == 8

    without = _clone(k8s_api_mode="kubectl")
    api = _ClusterApi(without)
    api.nodes = [_node("gpu-a", 8, cordoned=True)]
    api.cluster_pods = []
    probe = _driver(without, api).probe_capacity(
        MachineInfo(name="pool", host="", driver="k8s", node_selector=POOL)
    )
    assert probe["gpu_free"] == 0, "a cordon we do not tolerate keeps our pods off"


# -- sessions on a pool -------------------------------------------------------------


async def _make_pool(
    http, driver, *, node_gpu_max: int = 8, gpus_in_container: bool = False
) -> None:
    driver.name = "k8s"  # the supervisor hands k8s machines to this fake
    async with http.db() as session:
        # Delegated-only, as the API registers a policy by default.
        (await session.get(Policy, 1)).gpus_in_container = gpus_in_container
        machine = await session.get(Machine, 1)
        machine.driver = "k8s"
        machine.node_selector = POOL
        machine.gpu_count = 24
        machine.node_count = 3
        machine.node_gpu_max = node_gpu_max
        await session.commit()


async def test_a_session_takes_a_share_of_a_pool_not_the_whole_pool(night):  # noqa: F811
    supervisor, driver, http = night
    await _make_pool(http, driver)
    policy = await _boot(supervisor, driver, http)

    hardware = (await policy.get("/session")).json()["hardware"]
    # The widest config in the space is tp 4: four slots, not 24.
    assert hardware["gpu_indices"] == [0, 1, 2, 3]
    assert hardware["pool"] is True
    assert hardware["node_gpu_max"] == 8


async def test_a_policy_running_its_own_engines_stays_off_pools(night):  # noqa: F811
    # Its engines run in its own pod, so all its cards must be on one node.
    supervisor, driver, http = night
    await _make_pool(http, driver, gpus_in_container=True)
    for _ in range(3):
        supervisor.tick()
    row = await _session_row(http)
    assert row.machine_id is None
    assert not driver.workloads


async def test_campaigns_share_a_pool_without_asking_to(night):  # noqa: F811
    supervisor, driver, http = night
    await _make_pool(http, driver)
    async with http.db() as session:
        first = await session.get(Campaign, 1)
        first.share_machine = False  # would take a whole machine for itself
        session.add(Campaign(
            id=2, owner_id=1, name="second", engine="sglang", image="sglang:test",
            model_path="/models/m", served_model_name="m", search_space=SPACE,
            objective={"target_metric": "score_total"}, share_machine=False,
            status=CampaignStatus.ACTIVE.value, machine_names=["gpu-1"],
            run_baseline_canary=False, window_start=first.window_start,
            window_end=first.window_end, policy_id=1,
            policy_settings={"max_contenders": 1, "cards": 2},
        ))
        await session.commit()

    for _ in range(3):
        supervisor.tick()
    async with http.db() as session:
        rows = (await session.execute(select(PolicySession))).scalars().all()
    assert {r.campaign_id for r in rows if r.machine_id == 1} == {1, 2}
    by_campaign = {r.campaign_id: set(r.gpu_indices) for r in rows}
    assert len(by_campaign[1]) == 4 and len(by_campaign[2]) == 2
    assert not by_campaign[1] & by_campaign[2]


async def test_a_launch_no_node_could_hold_is_refused(night):  # noqa: F811
    supervisor, driver, http = night
    await _make_pool(http, driver, node_gpu_max=2)
    policy = await _boot(supervisor, driver, http)
    refused = await policy.post(
        "/launches", json={"engine_args": {"tp_size": 4}, "gpu_indices": [0, 1, 2, 3]},
        **{"Idempotency-Key": "wide"},
    )
    assert refused.status_code == 422
    assert "largest node in gpu-1 has 2" in refused.json()["detail"]


async def test_a_launch_waiting_for_cards_reads_as_queued_with_the_reason(night):  # noqa: F811
    supervisor, driver, http = night
    await _make_pool(http, driver)
    policy = await _boot(supervisor, driver, http)
    launched = await policy.post(
        "/launches", json={"engine_args": {"tp_size": 2}, "gpu_indices": [0, 1]},
        **{"Idempotency-Key": "q"},
    )
    run_id = launched.json()["id"]
    since = datetime.now(UTC) - timedelta(minutes=40)
    async with http.db() as session:
        run = await session.get(Run, run_id)
        run.status = RunStatus.WAITING_READY.value
        run.waiting_since = since
        session.add(Event(actor="worker", kind="run_waiting_for_cluster", run_id=run_id,
                          payload={"reason": "0/9 nodes are available: "
                                             "3 Insufficient nvidia.com/gpu"}))
        await session.commit()

    status = (await policy.get(f"/launches/{run_id}")).json()
    assert status["status"] == "queued"
    assert "Insufficient nvidia.com/gpu" in status["waiting_reason"]
    assert status["waiting_since"]

    # Placed: launching again, with nothing left to explain.
    async with http.db() as session:
        (await session.get(Run, run_id)).waiting_since = None
        await session.commit()
    status = (await policy.get(f"/launches/{run_id}")).json()
    assert status["status"] == "launching"
    assert status["waiting_reason"] == ""


async def test_a_contender_that_never_got_cards_is_skipped_not_failed(night):  # noqa: F811
    supervisor, driver, http = night
    await _boot(supervisor, driver, http)
    row = await _session_row(http)
    async with http.db() as session:
        candidate = Candidate(campaign_id=1, config={"tp_size": 2}, config_hash="h-wait")
        session.add(candidate)
        await session.flush()
        run = Run(
            campaign_id=1, candidate_id=candidate.id, machine_id=1,
            kind=RunKind.EXPERIMENT.value, status=RunStatus.KILLED.value,
            waiting_since=datetime.now(UTC) - timedelta(hours=2),
            error="the window ended",
        )
        session.add(run)
        await session.flush()
        session.add(PolicyContender(
            session_id=row.id, campaign_id=1, rank=1, status=ContenderStatus.SERVING.value,
            config_hash="h-wait", launch_spec={"engine_args": {"tp_size": 2}},
            run_id=run.id, candidate_id=candidate.id,
        ))
        session_row = await session.get(PolicySession, row.id)
        session_row.status = PolicySessionStatus.VALIDATING.value
        await session.commit()

    supervisor.tick()
    async with http.db() as session:
        contender = (await session.execute(select(PolicyContender))).scalars().one()
    assert contender.status == ContenderStatus.SKIPPED.value
    assert "no free cards" in contender.skip_reason


# -- waiting is the cluster's answer, read early ------------------------------------


class _QueuedDriver(FakeDriver):
    """A pod the cluster has not placed: pulling, with the scheduler's reason."""

    def __init__(self, reason):
        super().__init__()
        self.reason = reason
        self.placed = False

    def state(self, handle):
        if handle.container_name in self._gone:
            return DeploymentState.GONE
        return DeploymentState.READY if self.placed else DeploymentState.STARTING

    def startup_status(self, handle):
        return {"phase": "pulling", "running_since": None}

    def failure_reason(self, handle):
        return None if self.placed else self.reason


def _age_run(factory, minutes: float) -> None:
    with factory() as session:
        run = session.scalars(select(Run)).first()
        run.started_at = datetime.now(UTC) - timedelta(minutes=minutes)
        session.commit()


def test_a_run_waiting_for_cards_says_so_at_once_and_never_times_out():
    supervisor, factory = make_supervisor(bench=PassEvaluator(polls_until_done=50))
    supervisor.driver = _QueuedDriver(
        ("unschedulable", "0/9 nodes are available: 3 Insufficient nvidia.com/gpu")
    )
    supervisor.tick()  # launched
    _age_run(factory, 1)
    supervisor.tick()
    with factory() as session:
        run = session.scalars(select(Run)).first()
        assert run.waiting_since is not None  # a minute in, not 45
        assert run.status == RunStatus.WAITING_READY.value

    # Hours later it is still waiting, not failed: the cluster is just full.
    _age_run(factory, 600)
    supervisor.tick()
    with factory() as session:
        run = session.scalars(select(Run)).first()
        assert run.status == RunStatus.WAITING_READY.value

    # Cards free up: the run starts now, and its clocks with it.
    supervisor.driver.placed = True
    supervisor.tick()
    with factory() as session:
        run = session.scalars(select(Run)).first()
        assert run.waiting_since is None
        started = run.started_at.replace(tzinfo=UTC)
        assert datetime.now(UTC) - started < timedelta(minutes=1)
        assert session.scalars(select(Event).where(Event.kind == "run_placed")).first()
        campaign = session.get(Campaign, 1)
        # The startup sample is the time since placement, not the ten hours queued.
        assert max((campaign.run_timing or {}).get("startup", [0])) < 1


def test_a_run_no_node_could_hold_fails_at_once():
    supervisor, factory = make_supervisor()
    supervisor.driver = _QueuedDriver(
        ("unplaceable", "pod asks for 16 x nvidia.com/gpu but the largest node has 8")
    )
    supervisor.tick()
    _age_run(factory, 1)
    supervisor.tick()
    with factory() as session:
        run = session.scalars(select(Run)).first()
        assert run.status == RunStatus.FAILED.value
        assert run.failure_class == "unplaceable"
        assert "largest node has 8" in run.error


# -- a pool is one card type --------------------------------------------------------


async def test_a_pool_spanning_two_card_types_cannot_be_leased(client, monkeypatch):  # noqa: F811
    import app.control.launch as launch

    class _Mixed:
        def probe_capacity(self, machine):
            return {"supported": True, "node_count": 2, "gpu_count": 16, "gpu_type": "",
                    "gpu_types": ["A100", "H800"], "node_gpu_max": 8, "gpu_free": 4,
                    "warnings": []}

    monkeypatch.setattr(launch, "get_driver", lambda name, **kw: _Mixed())
    async with client.db() as session:
        session.add(Machine(name="gpus", host="", driver="k8s",
                            node_selector="nvidia.com/gpu.present=true", gpu_count=16))
        await session.commit()
    response = await client.post("/api/machines/lease", json={"name": "gpus"})
    assert response.status_code == 409
    assert "different GPU types (A100, H800)" in response.json()["detail"]

    # One type: leased, with what the cluster said recorded on the machine.
    async with client.db() as session:
        machine = (await session.execute(select(Machine))).scalars().one()
        machine.node_selector = POOL
        await session.commit()
    _Mixed.probe_capacity = lambda self, machine: {
        "supported": True, "node_count": 2, "gpu_count": 16, "gpu_type": "H800",
        "gpu_types": ["H800"], "node_gpu_max": 8, "gpu_free": 5, "warnings": []}
    assert (await client.post("/api/machines/lease", json={"name": "gpus"})).status_code == 200
    async with client.db() as session:
        machine = (await session.execute(select(Machine))).scalars().one()
    assert (machine.node_count, machine.node_gpu_max, machine.gpu_free) == (2, 8, 5)


class _NoNodesApi(FakeK8sApi):
    quotas: list[dict] = []

    def list(self, resource, label_selector=""):
        if resource == "nodes":
            raise RuntimeError("nodes is forbidden")
        if resource == "resourcequotas":
            return list(self.quotas)
        return super().list(resource, label_selector)


def test_without_node_access_the_namespace_quota_is_the_capacity():
    from app.hardware import record_capacity

    settings = _clone(k8s_api_mode="kubectl")
    api = _NoNodesApi(settings)
    api.quotas = [{"status": {"hard": {"requests.nvidia.com/gpu": "16", "pods": "50"},
                              "used": {"requests.nvidia.com/gpu": "6"}}}]
    machine = MachineInfo(name="pool", host="", driver="k8s", node_selector=POOL)
    probe = _driver(settings, api).probe_capacity(machine)
    assert probe["gpu_count"] == 16 and probe["gpu_free"] == 10
    # Node sizes are unknown: the node-fit check lets every pod through.
    assert probe["node_gpu_max"] == 0
    assert any("namespace's GPU quota" in w for w in probe["warnings"])
    row = Machine(name="pool", host="", driver="k8s", gpu_count=0)
    assert record_capacity(row, probe) and row.gpu_count == 16

    # No quota either: nothing to go by, and the row is left alone.
    api.quotas = []
    probe = _driver(settings, api).probe_capacity(machine)
    assert probe["gpu_count"] == 0 and not record_capacity(row, probe)
    assert row.gpu_count == 16


def test_a_pod_must_fit_a_node_not_the_whole_launch():
    from types import SimpleNamespace

    import pytest
    from fastapi import HTTPException

    from app.api.policy_sessions import _check_fits_a_node
    from app.control.recipe.campaign import config_cards, config_pod_cards
    from app.control.recipe.importer import parse
    from tests.test_recipe_publish import PD

    recipe = parse(PD.read_text()).recipe
    campaign = SimpleNamespace(recipe=recipe.model_dump(), search_space={})
    total, widest = config_cards(campaign, {}), config_pod_cards(campaign, {})
    assert total > widest > 0  # a pd recipe spreads over several pods
    pool = SimpleNamespace(name="pool", driver="k8s", node_selector=POOL, node_gpu_max=widest)
    _check_fits_a_node(pool, widest)
    pool.node_gpu_max = widest - 1
    with pytest.raises(HTTPException):
        _check_fits_a_node(pool, widest)
