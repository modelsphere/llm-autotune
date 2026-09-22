"""RunNode: the per-machine binding a run became when multi-node arrived.

Two things are being protected here:

  * the invariant that every run has a rank-0 node, kept by a mapper event so
    the API, the worker, the baseline canary, policy launches and test doubles
    cannot each forget it differently;
  * the time-scoped grouping contract — a node group is a named topology, NOT a
    reservation. Members stay usable for single-node work, and a machine is
    taken only while a gang is actually deployed on it.
"""

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.control.orchestrator.supervisor import Supervisor
from app.control.run_nodes import machine_hosts_live_gang, nodes_of
from app.db.base import Base
from app.db.models import (
    BaselineStatus,
    Campaign,
    CampaignStatus,
    Candidate,
    Machine,
    MachineState,
    Run,
    RunNode,
    RunStatus,
    User,
)
from tests.fakes import NullDriver, StubEvaluator


def _db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _seed(factory, *, pinned: list[str]) -> None:
    with factory() as session:
        session.add(User(id=1, username="u", password_hash="x"))
        for machine_id, name in ((1, "gpu-01"), (2, "gpu-02")):
            session.add(
                Machine(
                    id=machine_id, name=name, host=f"10.0.0.{machine_id}",
                    state=MachineState.AVAILABLE.value, gpu_count=8,
                    baseline_status=BaselineStatus.CLEARED.value,
                )
            )
        session.add(
            Campaign(
                id=1, owner_id=1, name="single", engine="sglang", image="img",
                model_path="/models/m", served_model_name="m",
                search_space={"grid": {"tp_size": [2]}},
                machine_names=pinned,
                status=CampaignStatus.ACTIVE.value,
            )
        )
        # A second campaign carries the gang run; DRAFT so the tick does not
        # plan any work for it. Its run is what occupies the machines.
        session.add(
            Campaign(
                id=2, owner_id=1, name="gang-owner", engine="sglang", image="img",
                model_path="/models/m", served_model_name="m",
                search_space={}, status=CampaignStatus.DRAFT.value,
            )
        )
        session.commit()


def _gang(factory, *, status: str = RunStatus.BENCHING.value) -> int:
    """A live two-node run: rank 0 on gpu-01, rank 1 on gpu-02.

    Built by hand because the driver cannot launch a gang yet — this is the
    data shape step 4 will produce, and the scheduler's grouping rules already
    have to read it correctly.
    """
    with factory() as session:
        candidate = Candidate(
            campaign_id=2, config={"tp_size": 8}, config_hash="gang", status="exhausted"
        )
        session.add(candidate)
        session.flush()
        run = Run(
            campaign_id=2, candidate_id=candidate.id, machine_id=1,
            gpu_indices=[0, 1, 2, 3], service_port=28200, status=status,
            node_group="pair",
        )
        session.add(run)
        session.flush()
        # The mapper event wrote rank 0; a gang's worker is rank 1 on machine 2.
        session.add(
            RunNode(
                run_id=run.id, machine_id=2, rank=1, is_master=False,
                gpu_indices=[0, 1, 2, 3], container_name=f"autotune-run-{run.id}-r1",
                status=status,
            )
        )
        session.commit()
        return run.id


def _supervisor(factory) -> Supervisor:
    supervisor = Supervisor(
        session_factory=factory, driver_name="ssh_docker",
        health_evaluator=StubEvaluator(), bench_evaluator=StubEvaluator(),
    )
    supervisor.driver = NullDriver()
    return supervisor


# -- the model invariant ------------------------------------------------------


def test_every_run_gets_a_rank0_master_node():
    factory = _db()
    _seed(factory, pinned=[])
    with factory() as session:
        candidate = Candidate(campaign_id=1, config={"tp_size": 2}, config_hash="a", status="valid")
        session.add(candidate)
        session.flush()
        run = Run(
            campaign_id=1, candidate_id=candidate.id, machine_id=1,
            gpu_indices=[4, 5], service_port=28300, status=RunStatus.PENDING.value,
        )
        session.add(run)
        session.commit()
        run_id = run.id

    with factory() as session:
        nodes = nodes_of(session, run_id)
        assert len(nodes) == 1
        node = nodes[0]
        assert (node.rank, node.is_master) == (0, True)
        assert node.machine_id == 1
        assert node.gpu_indices == [4, 5]
        assert node.service_port == 28300
        assert node.status == RunStatus.PENDING.value


def test_the_master_node_follows_the_run_after_launch():
    """container_name and endpoint_url are only known after the row exists and
    the driver has run; the node must not keep the empty values it was born
    with."""
    factory = _db()
    _seed(factory, pinned=[])
    with factory() as session:
        candidate = Candidate(campaign_id=1, config={"tp_size": 2}, config_hash="b", status="valid")
        session.add(candidate)
        session.flush()
        run = Run(campaign_id=1, candidate_id=candidate.id, machine_id=1, gpu_indices=[0, 1])
        session.add(run)
        session.flush()
        run.container_name = f"autotune-run-{run.id}"
        run.endpoint_url = "http://10.0.0.1:28200"
        run.launch_command = "docker run ..."
        run.status = RunStatus.LAUNCHING.value
        session.commit()
        run_id = run.id

    with factory() as session:
        node = nodes_of(session, run_id)[0]
        assert node.container_name == f"autotune-run-{run_id}"
        assert node.endpoint_url == "http://10.0.0.1:28200"
        assert node.launch_command == "docker run ..."
        assert node.status == RunStatus.LAUNCHING.value


# -- the grouping contract ----------------------------------------------------


def test_a_single_node_run_is_not_a_gang():
    factory = _db()
    _seed(factory, pinned=[])
    with factory() as session:
        candidate = Candidate(campaign_id=1, config={"tp_size": 2}, config_hash="c", status="valid")
        session.add(candidate)
        session.flush()
        session.add(
            Run(campaign_id=1, candidate_id=candidate.id, machine_id=1, gpu_indices=[0, 1])
        )
        session.commit()
    with factory() as session:
        assert machine_hosts_live_gang(session, 1) is False


def test_a_live_gang_occupies_every_member_and_a_finished_one_does_not():
    factory = _db()
    _seed(factory, pinned=[])
    _gang(factory)

    with factory() as session:
        # Both the master's machine and the worker's are occupied by the gang.
        assert machine_hosts_live_gang(session, 1) is True
        assert machine_hosts_live_gang(session, 2) is True

    with factory() as session:
        run = session.get(Run, 1)
        run.status = RunStatus.SUCCEEDED.value
        session.commit()

    with factory() as session:
        # Different time window, same machines: grouping does not reserve them.
        assert machine_hosts_live_gang(session, 1) is False
        assert machine_hosts_live_gang(session, 2) is False


def test_a_member_of_a_live_gang_takes_no_single_node_work():
    factory = _db()
    _seed(factory, pinned=["gpu-02"])  # the campaign wants the WORKER machine
    _gang(factory)
    supervisor = _supervisor(factory)

    with factory() as session:
        assert supervisor._startable_slots(session, session.get(Campaign, 1)) == 0

    supervisor.tick()
    with factory() as session:
        placed = session.scalars(select(Run).where(Run.campaign_id == 1)).all()
        assert placed == [], "nothing may be placed on a machine a gang is using"


def test_a_member_is_an_ordinary_machine_when_no_gang_is_live():
    """The requirement, in one test: grouping two boxes does not stop either
    from running single-node work — it only stops the two from overlapping."""
    factory = _db()
    _seed(factory, pinned=["gpu-02"])
    run_id = _gang(factory, status=RunStatus.SUCCEEDED.value)
    supervisor = _supervisor(factory)

    with factory() as session:
        assert supervisor._startable_slots(session, session.get(Campaign, 1)) == 8

    supervisor.tick()
    with factory() as session:
        placed = session.scalars(
            select(Run).where(Run.campaign_id == 1, Run.id != run_id)
        ).all()
        assert len(placed) == 1
        assert placed[0].machine_id == 2  # the member the campaign pinned


def test_benching_a_gang_is_still_live():
    """A gang mid-benchmark is occupying its machines, not parked."""
    factory = _db()
    _seed(factory, pinned=[])
    _gang(factory, status=RunStatus.BENCHING.value)
    with factory() as session:
        assert machine_hosts_live_gang(session, 2) is True
