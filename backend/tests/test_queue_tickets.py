"""The machine queue's tickets: one order for cards and a cluster's fixed port.

A finished run's port Service was made again by its own teardown and held a
cluster's one NodePort; the next two runs each retried the port every tick,
unordered and labelled "waiting for cards". Now:
every waiter keeps a ticket, cards and the port are handed out together at
placement in ticket order, a waiter that cannot be served holds the machines
it needs (and every machine sharing their port) against later tickets, a
Service AutoTune left behind for a finished run is released, and each waiter
says what it waits for and where it is in line.
"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.control.launch.base import (
    DeploymentDriver,
    DeploymentHandle,
    DeploymentState,
    LaunchDeferred,
    WaitingFor,
)
from app.control.orchestrator import queue_tickets
from app.control.orchestrator.lifecycle import now
from app.db.models import TERMINAL_RUN_STATES as TERMINAL
from app.db.models import (
    Campaign,
    CampaignStatus,
    Candidate,
    CandidateStatus,
    Cluster,
    Event,
    Machine,
    MachineState,
    QueueTicket,
    Run,
    RunKind,
    RunStatus,
)
from tests.test_node_ports import _kubeconfig
from tests.test_supervisor import make_supervisor


def _fixed_port_fleet(factory):
    """Machine 1 ("gpu-01") and machine 8 ("m8"): two registrations of one
    cluster (same node host), one fixed NodePort between them; machine 9 on
    another cluster."""
    from app.core.secrets import encrypt_secret

    with factory() as s:
        for cid, host in ((7, "198.51.100.84"), (8, "198.51.100.84"), (9, "203.0.113.9")):
            s.add(Cluster(
                id=cid, name=f"c{cid}", workload_kind="deployment", endpoint="dns",
                node_host=host, service_nodeport=31080, namespace="runs", api_mode="client",
                kubeconfig_enc=encrypt_secret(_kubeconfig(f"https://{cid}.example:6443")),
            ))
        m = s.get(Machine, 1)
        m.driver, m.cluster_id = "k8s", 7
        for mid, cid in ((8, 8), (9, 9)):
            s.add(Machine(id=mid, name=f"m{mid}", host="", driver="k8s", cluster_id=cid,
                          gpu_count=8, state=MachineState.AVAILABLE.value))
        s.commit()


def _campaign(s, cid, machine_name, *, ready_at=None):
    s.add(Campaign(id=cid, owner_id=1, name=f"c{cid}", engine="sglang", image="img",
                   model_path="/models/m", served_model_name="m",
                   search_space={"grid": {"tp_size": [2]}}, machine_names=[machine_name],
                   share_machine=False, status=CampaignStatus.ACTIVE.value))
    s.add(Candidate(id=cid * 100, campaign_id=cid, config={"tp_size": 2},
                    config_hash=f"h{cid}", status=CandidateStatus.VALID.value,
                    created_at=ready_at or now()))


def _whole_machine_held(s, machine_id, rid):
    """Something holding a machine's cards but not the port: a measurement
    of an endpoint nobody here launched, of unknown width."""
    s.add(Candidate(id=rid, campaign_id=1, config={}, config_hash=f"x{rid}",
                    status=CandidateStatus.EXHAUSTED.value))
    s.add(Run(id=rid, campaign_id=1, candidate_id=rid, machine_id=machine_id,
              kind=RunKind.EXTERNAL.value, status=RunStatus.BENCHING.value))


def _supervisor():
    supervisor, factory = make_supervisor()
    supervisor._driver_for = lambda machine: supervisor.driver
    with factory() as s:
        s.get(Campaign, 1).status = CampaignStatus.DONE.value  # its own scenario's
        s.commit()
    return supervisor, factory


def _ticket(s, waiter):
    return s.scalars(select(QueueTicket).where(QueueTicket.waiter == waiter)).first()


def test_a_waiter_keeps_its_ticket_and_one_served_goes_to_the_back():
    supervisor, factory = _supervisor()
    t0 = now() - timedelta(minutes=5)
    with factory() as s:
        _campaign(s, 2, "gpu-01", ready_at=t0)
        _campaign(s, 3, "gpu-01", ready_at=t0 + timedelta(minutes=1))
        s.commit()
    supervisor.tick()
    with factory() as s:
        assert [r.campaign_id for r in s.scalars(select(Run)).all()] == [2]
        assert _ticket(s, "campaign:2") is None, "served: its ticket is given up"
        waiting = _ticket(s, "campaign:3")
        assert waiting.position == 1 and waiting.waiting_for == WaitingFor.CARDS
        assert "gpu-01" in waiting.reason
        first_id = waiting.id
    supervisor.tick()
    with factory() as s:
        assert _ticket(s, "campaign:3").id == first_id, "the same ticket, pass after pass"


def test_the_port_goes_in_ticket_order_whichever_registration_asks():
    """The older campaign waits for cards on one registration of the cluster;
    the newer one has free cards on the other. The newer one must not take
    the cluster's port in between, or the older could wait behind it forever."""
    supervisor, factory = _supervisor()
    _fixed_port_fleet(factory)
    t0 = now() - timedelta(minutes=5)
    with factory() as s:
        _whole_machine_held(s, 1, 900)
        _campaign(s, 2, "gpu-01", ready_at=t0)
        _campaign(s, 3, "m8", ready_at=t0 + timedelta(minutes=1))
        _campaign(s, 4, "m9", ready_at=t0 + timedelta(minutes=2))  # another cluster
        s.commit()
    supervisor.tick()
    with factory() as s:
        placed = {r.campaign_id for r in s.scalars(select(Run)).all() if r.id != 900}
        assert placed == {4}, "only the other cluster's waiter goes"
        older, newer = _ticket(s, "campaign:2"), _ticket(s, "campaign:3")
        assert (older.position, older.waiting_for) == (1, WaitingFor.CARDS)
        assert (newer.position, newer.waiting_for) == (2, WaitingFor.NODE_PORT)
        assert "behind campaign “c2”" in newer.reason

        s.get(Run, 900).status = RunStatus.SUCCEEDED.value
        s.commit()
    supervisor.tick()
    with factory() as s:
        runs = {r.campaign_id: r for r in s.scalars(select(Run)).all() if r.id != 900}
        assert 2 in runs and 3 not in runs, "the older one gets the cards and the port"
        newer = _ticket(s, "campaign:3")
        assert newer.position == 1 and newer.waiting_for == WaitingFor.NODE_PORT
        assert f"held by run {runs[2].id}" in newer.reason


def test_a_finished_runs_left_over_port_service_is_released_and_the_next_run_starts():
    """k8s refuses the port, the platform knows no holder, and
    the Service holding it is AutoTune's own for a run that is over."""
    supervisor, factory = _supervisor()
    _fixed_port_fleet(factory)
    with factory() as s:
        s.add(Candidate(id=50, campaign_id=1, config={}, config_hash="h50",
                        status=CandidateStatus.EXHAUSTED.value))
        s.add(Run(id=50, campaign_id=1, candidate_id=50, machine_id=1,
                  status=RunStatus.FAILED.value))
        _campaign(s, 2, "gpu-01")
        s.commit()
    orphan = {"autotune-run-50-np": 50}
    real_launch = supervisor.driver.launch

    def launch(spec):
        if orphan:
            name, run = next(iter(orphan.items()))
            raise LaunchDeferred(f"node port held by Service runs/{name}", WaitingFor.NODE_PORT,
                                 holder_service=name, holder_run=run)
        return real_launch(spec)

    def release(service, run_id):
        assert orphan.pop(service) == run_id
        return True

    supervisor.driver.launch = launch
    supervisor.driver.release_orphan_port = release
    supervisor.tick()  # placed; refused; the orphan released
    with factory() as s:
        run = s.scalars(select(Run).where(Run.campaign_id == 2)).one()
        assert run.status == RunStatus.PENDING.value
        assert run.waiting_for == WaitingFor.NODE_PORT
        kinds = [e.kind for e in s.scalars(select(Event).where(Event.run_id == run.id))]
        assert "orphan_port_released" in kinds
    supervisor.tick()
    with factory() as s:
        run = s.scalars(select(Run).where(Run.campaign_id == 2)).one()
        assert run.status != RunStatus.PENDING.value
        assert (run.waiting_for, run.waiting_since) == ("", None)


@pytest.mark.parametrize("holder_state", ["live", "tearing_down", "unknown", "itself"])
def test_a_port_service_that_may_still_be_in_use_is_waited_for(holder_state):
    """Released only when it is provably left over: a run this database knows,
    over, and torn down. Anything else is waited for, never touched."""
    supervisor, factory = _supervisor()
    released = []
    supervisor.driver.release_orphan_port = lambda service, run_id: released.append(service)
    with factory() as s:
        _campaign(s, 2, "gpu-01")
        s.add(Run(id=60, campaign_id=2, candidate_id=200, machine_id=1,
                  status=RunStatus.PENDING.value))
        if holder_state in ("live", "tearing_down"):
            s.add(Run(id=50, campaign_id=2, candidate_id=200, machine_id=1,
                      status=(RunStatus.BENCHING.value if holder_state == "live"
                              else RunStatus.FAILED.value),
                      teardown_pending=holder_state == "tearing_down"))
        s.commit()
        holder = 60 if holder_state == "itself" else 50
        exc = LaunchDeferred("held", WaitingFor.NODE_PORT,
                             holder_service=f"autotune-run-{holder}-np", holder_run=holder)
        supervisor._reclaim_orphan_port(s, s.get(Run, 60), supervisor.driver, exc)
    assert released == []


def test_a_paused_campaign_gives_back_a_run_that_only_waits_for_the_port():
    supervisor, factory = _supervisor()
    _fixed_port_fleet(factory)
    with factory() as s:
        _campaign(s, 2, "gpu-01")
        s.commit()

    def launch(spec):
        raise LaunchDeferred("held elsewhere", WaitingFor.NODE_PORT)

    supervisor.driver.launch = launch
    supervisor.tick()
    with factory() as s:
        run = s.scalars(select(Run).where(Run.campaign_id == 2)).one()
        assert run.waiting_for == WaitingFor.NODE_PORT
        s.get(Campaign, 2).status = CampaignStatus.PAUSED.value
        s.commit()
    supervisor.tick()
    with factory() as s:
        run = s.get(Run, run.id)
        assert run.status == RunStatus.KILLED.value and run.failure_class == "released"
        assert s.get(Candidate, 200).status == CandidateStatus.VALID.value
        assert not run.teardown_pending


def test_a_ticket_is_let_go_once_its_waiter_has_left_but_not_for_a_failed_listing():
    from app.control.orchestrator.queue_tickets import TicketBook

    supervisor, factory = _supervisor()
    with factory() as s:
        book = TicketBook(s)
        old = now() - queue_tickets.GRACE - timedelta(minutes=1)
        for waiter in ("campaign:2", "plugin:example:7"):
            book.take(waiter, (old, 1, 2), label=waiter).seen_at = old
        s.commit()
    with factory() as s:
        book = TicketBook(s)
        book.keep("plugin:example:")
        book.sweep()
        s.commit()
        assert _ticket(s, "campaign:2") is None
        assert _ticket(s, "plugin:example:7") is not None


def test_a_waiting_run_says_what_it_waits_for_and_a_placed_one_clears_it():
    supervisor, factory = _supervisor()
    _fixed_port_fleet(factory)
    with factory() as s:
        _campaign(s, 2, "gpu-01")
        s.commit()
    deferrals = iter([WaitingFor.IMAGE, WaitingFor.NODE_PORT])
    real_launch = supervisor.driver.launch

    def launch(spec):
        kind = next(deferrals, None)
        if kind is not None:
            raise LaunchDeferred(f"waiting: {kind}", kind)
        return real_launch(spec)

    supervisor.driver.launch = launch
    supervisor.tick()
    with factory() as s:
        run = s.scalars(select(Run).where(Run.campaign_id == 2)).one()
        assert (run.waiting_for, run.waiting_reason) == ("image", "waiting: image")
    supervisor.tick()
    with factory() as s:
        run = s.get(Run, run.id)
        assert run.waiting_for == "node_port"
        events = [e.payload for e in s.scalars(select(Event).where(
            Event.run_id == run.id, Event.kind == "run_waiting_for_cluster"))]
        assert [e["waiting_for"] for e in events] == ["image", "node_port"]
    supervisor.tick()
    with factory() as s:
        run = s.get(Run, run.id)
        assert run.status != RunStatus.PENDING.value
        assert (run.waiting_for, run.waiting_reason, run.waiting_since) == ("", "", None)


async def test_a_campaign_reads_back_its_place_in_line():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.api.campaigns import _queue_of
    from app.db.base import Base

    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        campaign = Campaign(id=2, owner_id=1, name="c2", engine="sglang", image="img",
                            model_path="/m", served_model_name="m", search_space={},
                            status=CampaignStatus.ACTIVE.value)
        s.add(campaign)
        t = now()
        s.add(QueueTicket(waiter="campaign:2", label="c2", campaign_id=2, arrival=t,
                          issued_at=t, seen_at=t, position=2, waiting_for="node_port",
                          reason="behind campaign “c1”"))
        await s.commit()
        queue = await _queue_of(s, campaign)
        assert (queue.position, queue.waiting_for) == (2, "node_port")
        ticket = (await s.execute(select(QueueTicket))).scalars().one()
        ticket.seen_at = t - queue_tickets.FRESH - timedelta(seconds=1)
        await s.commit()
        assert await _queue_of(s, campaign) is None, "a ticket no recent pass saw is stale"


# -- everything at once ---------------------------------------------------------


class _ChaosDriver:
    """A cluster with one fixed NodePort, and things going wrong at random:
    the port refused (by an outsider, or by a Service a finished run left
    behind), engines crashing, teardowns that linger and port Services that
    outlive their run. It refuses a second engine on the port, as k8s would."""

    def __init__(self, inner, rng):
        self.inner, self.rng = inner, rng
        self.chaos = True
        self.port_owner: str | None = None  # the engine reached on the port
        self.orphan: tuple[str, int] | None = None  # (service, run id) left behind
        self.lingering: dict[str, int] = {}  # container -> ticks left
        self.leftover: dict[str, int] = {}  # container -> ticks its Service outlives it
        self.outsider = 0  # ticks a Service outside the platform holds the port

    def __getattr__(self, name):
        return getattr(self.inner, name)

    # Cannot queue a pod: what the pause path asks before looking.
    placement = DeploymentDriver.placement

    def launch(self, spec):
        if self.outsider > 0:
            raise LaunchDeferred("held by a Service outside", WaitingFor.NODE_PORT)
        if self.orphan is not None:
            name, run = self.orphan
            raise LaunchDeferred(f"held by Service runs/{name}", WaitingFor.NODE_PORT,
                                 holder_service=name, holder_run=run)
        if self.port_owner is not None and self.port_owner != spec.container_name:
            raise AssertionError(
                f"{spec.container_name} launched while {self.port_owner} holds the port")
        self.port_owner = spec.container_name
        return self.inner.launch(spec)

    def launch_nodes(self, spec):
        handle, command = self.launch(spec)
        return handle, [command]

    def state(self, handle):
        if self.chaos and self.rng.random() < 0.08:
            return DeploymentState.CRASHED
        return self.inner.state(handle)

    def teardown(self, handle):
        name = handle.container_name
        if self.chaos and self.rng.random() < 0.3:
            self.lingering[name] = self.rng.randint(1, 3)
        if self.chaos and self.rng.random() < 0.3:
            self.leftover[name] = self.rng.randint(1, 3)
        self.inner.teardown(handle)

    request_stop = teardown

    def attach(self, spec):
        name = spec.container_name
        if self.lingering.get(name, 0) > 0:
            self.lingering[name] -= 1
            return DeploymentHandle(driver="fake", container_name=name,
                                    machine=spec.machine, endpoint_url="")
        return self.inner.attach(spec)

    def is_gone(self, handle):
        return self.lingering.get(handle.container_name, 0) <= 0

    def release_leftovers(self, spec):
        name = spec.container_name
        if self.leftover.get(name, 0) > 0:
            self.leftover[name] -= 1
            return False
        if self.port_owner == name:
            # Gone with its Service; now and then the Service stays behind
            # anyway, and only the next launch finds out.
            self.port_owner = None
            if self.chaos and self.rng.random() < 0.2:
                self.orphan = (f"{name}-np", int(name.rsplit("-", 1)[1]))
        return True

    def release_orphan_port(self, service, run_id):
        assert self.orphan == (service, run_id), "only what the refusal named"
        self.orphan = None
        return True


def _port_holders(s):
    """Runs the platform counts as holding the cluster's port (machines 1, 8)."""
    return [
        r for r in s.scalars(select(Run).where(Run.machine_id.in_([1, 8]))).all()
        if r.kind not in (RunKind.BASELINE.value, RunKind.EXTERNAL.value)
        and (r.status not in {st.value for st in TERMINAL} or r.teardown_pending)
    ]




@pytest.mark.parametrize("seed", range(6))
def test_the_port_is_held_by_one_run_at_a_time_and_everyone_gets_it(seed):
    import random

    rng = random.Random(seed)
    supervisor, factory = _supervisor()
    _fixed_port_fleet(factory)
    chaos = _ChaosDriver(supervisor.driver, rng)
    supervisor.driver = chaos
    supervisor._driver_for = lambda machine: chaos
    t0 = now() - timedelta(minutes=30)
    with factory() as s:
        for cid, machine in ((2, "gpu-01"), (3, "m8"), (4, "gpu-01"), (5, "m8")):
            _campaign(s, cid, machine, ready_at=t0 + timedelta(minutes=cid))
            s.add(Candidate(id=cid * 100 + 1, campaign_id=cid, config={"tp_size": 1},
                            config_hash=f"h{cid}b", status=CandidateStatus.VALID.value,
                            created_at=t0))
        s.commit()

    for tick in range(120):
        if tick < 80:
            if rng.random() < 0.05:
                chaos.outsider = rng.randint(1, 4)
            with factory() as s:
                for campaign in s.scalars(select(Campaign).where(Campaign.id > 1)).all():
                    if campaign.status == CampaignStatus.ACTIVE.value and rng.random() < 0.03:
                        campaign.status = CampaignStatus.PAUSED.value
                    elif campaign.status == CampaignStatus.PAUSED.value and rng.random() < 0.3:
                        campaign.status = CampaignStatus.ACTIVE.value
                s.commit()
        else:
            chaos.chaos = False
            with factory() as s:
                for campaign in s.scalars(select(Campaign).where(
                        Campaign.status == CampaignStatus.PAUSED.value)).all():
                    campaign.status = CampaignStatus.ACTIVE.value
                s.commit()
        chaos.outsider = max(0, chaos.outsider - 1)
        supervisor.tick()
        with factory() as s:
            holders = _port_holders(s)
            assert len(holders) <= 1, (
                f"tick {tick}: {[(r.id, r.status, r.teardown_pending) for r in holders]}")

    with factory() as s:
        statuses = {c.id: c.status for c in s.scalars(select(Campaign).where(Campaign.id > 1))}
        live = [(r.id, r.status) for r in s.scalars(select(Run)).all()
                if r.status not in {st.value for st in TERMINAL} or r.teardown_pending]
        assert live == [], f"seed {seed}: stuck runs {live}"
        assert set(statuses.values()) == {CampaignStatus.DONE.value}, statuses
        # A Service left behind by the very last run stays until a run needs
        # the port; no engine holds it.
        assert chaos.port_owner is None
