"""Step 4: launching one deployment across several machines.

The driver-level tests stand in for ssh (`_ssh` is where every remote command
goes through) so the ORDER, the per-node commands and the teardown ordering are
exercised for real without a machine. The supervisor-level tests then show that
a group-pinned campaign actually becomes a gang — and that a group member is
still an ordinary single-node machine when no gang is on it.
"""

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.control.launch.base import (
    DeploymentState,
    LaunchSpec,
    MachineInfo,
    NodeAssignment,
)
from app.control.launch.ssh_docker import SshDockerDriver
from app.control.orchestrator.supervisor import Supervisor
from app.db.base import Base
from app.db.models import (
    BaselineStatus,
    Campaign,
    CampaignStatus,
    Candidate,
    LeaseState,
    Machine,
    MachineGroup,
    MachineGroupMember,
    MachineState,
    Run,
    RunNode,
    RunStatus,
    User,
)
from tests.fakes import StubEvaluator


class _Result:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


class _RecordingSsh:
    """Every remote command, recorded; success unless told otherwise."""

    def __init__(
        self,
        fail_on: str = "",
        not_running: set[str] | None = None,
        gone: set[str] | None = None,
    ):
        self.calls: list[tuple[str, str]] = []
        self.fail_on = fail_on
        self.not_running = not_running or set()
        self.gone = gone or set()

    def __call__(self, machine, remote_cmd: str, timeout: int = 60):
        self.calls.append((machine.name, remote_cmd))
        if self.fail_on and self.fail_on in remote_cmd:
            return _Result("", 1, "boom")
        # Model container existence: a `docker run` brings one into being, a
        # kill/rm takes it away. The idempotent `docker rm -f` that launch()
        # issues BEFORE the run must not leave the container looking absent, so
        # the run clears it again — which is exactly the real ordering.
        if "docker run" in remote_cmd and "--name" in remote_cmd:
            self.gone.discard(remote_cmd.split("--name", 1)[1].split()[0].strip("'"))
        for action in ("docker kill --signal=SIGTERM ", "docker rm -f "):
            if action in remote_cmd:
                self.gone.add(remote_cmd.split(action, 1)[1].split()[0].strip("'"))
        if ".State.Status" in remote_cmd:
            name = remote_cmd.rsplit(" ", 1)[-1]
            if name in self.gone:
                return _Result("", 1, "No such container")
            return _Result("exited" if name in self.not_running else "running")
        return _Result("", 0)

    def index(self, *needles: str) -> int:
        for position, (_, command) in enumerate(self.calls):
            if all(needle in command for needle in needles):
                return position
        raise AssertionError(f"no command matching {needles} in {self.calls!r}")

    def find(self, predicate) -> int:
        for position, (_, command) in enumerate(self.calls):
            if predicate(command):
                return position
        raise AssertionError(f"no command matched in {self.calls!r}")


def _driver(fail_on: str = "", not_running=None, gone=None):
    driver = SshDockerDriver()
    ssh = _RecordingSsh(fail_on, not_running, gone)
    driver._ssh = ssh  # the one seam every remote call goes through
    driver._endpoint_ready = lambda url: True
    return driver, ssh


def _pair_spec(run_id: int = 7) -> LaunchSpec:
    master = MachineInfo(name="node-1", host="10.0.0.1", data_host="192.168.9.1")
    worker = MachineInfo(name="node-2", host="10.0.0.2")
    nodes = [
        NodeAssignment(
            machine=master, gpu_indices=[0, 1, 2, 3], rank=0,
            container_name=f"autotune-run-{run_id}",
        ),
        NodeAssignment(
            machine=worker, gpu_indices=[0, 1, 2, 3], rank=1,
            container_name=f"autotune-run-{run_id}-r1",
        ),
    ]
    return LaunchSpec(
        run_id=run_id, machine=master, engine="sglang", image="img:1",
        model_path="/models/m", served_model_name="m",
        engine_args={"tp": 8}, nodes=nodes, nnodes=2, dist_port=29500,
        dist_init_addr="192.168.9.1:29500", gpu_indices=[0, 1, 2, 3],
    )


# -- the launch seam ----------------------------------------------------------


def test_launch_nodes_starts_the_master_then_the_worker():
    driver, ssh = _driver()
    handle, commands = driver.launch_nodes(_pair_spec())

    assert handle.container_name == "autotune-run-7"
    assert [node.container_name for node in handle.node_handles] == ["autotune-run-7-r1"]
    assert len(commands) == 2
    # The master is told it is rank 0 of two and where to rendezvous; the worker
    # is told the same world and its own rank.
    assert "--nnodes 2" in commands[0]
    assert "--node-rank 0" in commands[0]
    assert "--dist-init-addr 192.168.9.1:29500" in commands[0]
    assert "--node-rank 1" in commands[1]
    assert "autotune-run-7-r1" in commands[1]


def test_the_worker_waits_for_the_master_rendezvous():
    """Start master, wait for its socket, then start the worker — the order the
    whole feature hinges on."""
    driver, ssh = _driver()
    driver.launch_nodes(_pair_spec())

    master_run = ssh.find(lambda c: "docker run" in c and "--name autotune-run-7 " in c)
    rendezvous = ssh.index("grep -q ':29500$'")
    worker_run = ssh.find(lambda c: "docker run" in c and "--name autotune-run-7-r1" in c)
    assert master_run < rendezvous < worker_run


def test_a_worker_that_fails_to_start_takes_the_master_with_it():
    driver, ssh = _driver(fail_on="autotune-run-7-r1")
    try:
        driver.launch_nodes(_pair_spec())
    except RuntimeError as exc:
        assert "docker run failed" in str(exc)
    else:  # pragma: no cover - the launch must raise
        raise AssertionError("a failed worker launch must not return a handle")
    # The half-deployment is cleaned up rather than left holding cards.
    assert any(
        "docker rm -f autotune-run-7" in command for _, command in ssh.calls
    )


def test_teardown_removes_workers_before_the_master():
    driver, ssh = _driver()
    handle, _ = driver.launch_nodes(_pair_spec())
    ssh.calls.clear()
    driver.teardown(handle)
    worker_rm = ssh.find(lambda c: c.rstrip().endswith("docker rm -f autotune-run-7-r1"))
    master_rm = ssh.find(lambda c: c.rstrip().endswith("docker rm -f autotune-run-7"))
    assert worker_rm < master_rm


def test_a_dead_worker_makes_the_whole_gang_crashed():
    """Not "still starting" until the ready timeout: a worker that is gone can
    never rendezvous, and saying so is the difference between a diagnosable
    failure and an inference."""
    driver, _ = _driver(not_running={"autotune-run-7-r1"})
    handle, _ = driver.launch_nodes(_pair_spec())
    assert driver.state(handle) == DeploymentState.CRASHED
    assert driver.failure_reason(handle) == (
        "node_failed",
        "worker rank 1 (autotune-run-7-r1) on node-2 is crashed; the deployment "
        "cannot rendezvous",
    )


def test_a_gang_is_ready_when_its_master_answers():
    driver, _ = _driver()
    handle, _ = driver.launch_nodes(_pair_spec())
    assert driver.state(handle) == DeploymentState.READY


def test_is_gone_only_when_every_node_is():
    driver, ssh = _driver()
    handle, _ = driver.launch_nodes(_pair_spec())
    # The master's removal alone does not free the gang: the worker still holds
    # its cards, so the scheduler must not see the machine as free.
    ssh.gone = {"autotune-run-7"}
    ssh.calls.clear()
    assert driver.is_gone(handle) is False

    ssh.gone = {"autotune-run-7", "autotune-run-7-r1"}
    ssh.calls.clear()
    assert driver.is_gone(handle) is True


def test_attach_rebuilds_every_node_from_the_spec():
    spec = _pair_spec()
    got = SshDockerDriver()
    got._ssh = _RecordingSsh()
    got._endpoint_ready = lambda url: True
    handle = got.attach(spec)
    assert handle is not None
    assert handle.container_name == "autotune-run-7"
    assert [n.container_name for n in handle.node_handles] == ["autotune-run-7-r1"]
    # Each node's handle knows its own machine, which is what teardown addresses.
    assert [n.machine.name for n in handle.all_handles] == ["node-1", "node-2"]


def test_for_node_is_a_leaf_spec_not_a_plan():
    spec = _pair_spec()
    worker = spec.for_node(spec.nodes[1])
    assert worker.rank == 1
    assert worker.nodes == []
    assert worker.container_name == "autotune-run-7-r1"
    assert worker.machine.name == "node-2"
    # The rendezvous is the same fact for every rank.
    assert worker.dist_init_addr == "192.168.9.1:29500"
    assert worker.nnodes == 2


# -- a group-pinned campaign becomes a gang -----------------------------------


def _db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _seed(factory, *, pinned_group: bool, tp_values: list[int]) -> None:
    with factory() as session:
        session.add(User(id=1, username="u", password_hash="x"))
        for machine_id, name in ((1, "node-1"), (2, "node-2")):
            session.add(Machine(
                id=machine_id, name=name, host=f"10.0.0.{machine_id}",
                # The master has a rail address; the worker does not, so its
                # interior host falls back to `host`.
                data_host="192.168.9.1" if machine_id == 1 else "",
                state=MachineState.AVAILABLE.value, gpu_count=8, gpu_type="A100",
                baseline_status=BaselineStatus.CLEARED.value,
                lease_state=LeaseState.ACTIVE.value,
            ))
        group = MachineGroup(id=1, name="pair", driver="")
        session.add(group)
        session.flush()
        session.add(MachineGroupMember(group_id=1, machine_id=1, rank=0))
        session.add(MachineGroupMember(group_id=1, machine_id=2, rank=1))
        session.add(Campaign(
            id=1, owner_id=1, name="c", engine="sglang", image="img",
            model_path="/models/m", served_model_name="m",
            search_space={"grid": {"tp_size": tp_values}},
            node_group="pair" if pinned_group else "",
            status=CampaignStatus.ACTIVE.value,
        ))
        session.commit()


def _supervisor(factory, driver):
    supervisor = Supervisor(
        session_factory=factory, driver_name="ssh_docker",
        health_evaluator=StubEvaluator(), bench_evaluator=StubEvaluator(),
    )
    supervisor.driver = driver
    return supervisor


def test_a_group_campaign_places_a_gang_on_every_member_and_launches_it():
    factory = _db()
    # 16 cards across two 8-card boxes: exactly what the group is for.
    _seed(factory, pinned_group=True, tp_values=[16])
    driver, ssh = _driver()
    supervisor = _supervisor(factory, driver)

    supervisor.tick()

    with factory() as session:
        run = session.scalars(select(Run)).one()
        nodes = list(session.scalars(select(RunNode).order_by(RunNode.rank)))
        assert [n.rank for n in nodes] == [0, 1]
        assert [n.machine_id for n in nodes] == [1, 2]
        assert run.machine_id == 1  # the master
        assert run.node_group == "pair"
        assert run.dist_port >= 29500
        assert run.status == RunStatus.LAUNCHING.value
        # Each rank got its own cards and its own command on its own row.
        assert nodes[0].gpu_indices == [0, 1, 2, 3, 4, 5, 6, 7]
        assert nodes[1].gpu_indices == [0, 1, 2, 3, 4, 5, 6, 7]
        assert "--node-rank 0" in run.launch_command
        assert "--node-rank 1" in nodes[1].launch_command
        assert nodes[1].container_name == f"autotune-run-{run.id}-r1"
        # Both boxes are reserved for the gang.
        assert session.get(Machine, 1).state == MachineState.RESERVED.value
        assert session.get(Machine, 2).state == MachineState.RESERVED.value

    # And both containers were actually started, master first.
    assert ssh.index("docker run", "--name autotune-run-1") < ssh.index(
        "docker run", "--name autotune-run-1-r1"
    )


def test_a_gang_copies_the_nccl_socket_interface_to_gloo():
    """A gang needs two process groups on the same NIC. NCCL_SOCKET_IFNAME names
    it, but torch's Gloo backend needs GLOO_SOCKET_IFNAME too — and unset it
    resolves the container hostname, which every one of these boxes maps to
    loopback, so rank 0's Gloo store is unreachable and the workers die in
    `connectFullMesh`. The platform fills that in from the NCCL name."""
    factory = _db()
    _seed(factory, pinned_group=True, tp_values=[16])
    with factory() as session:
        session.get(MachineGroup, 1).nccl_env = {"NCCL_SOCKET_IFNAME": "enp86s0f0np0"}
        session.commit()
    driver, _ = _driver()
    supervisor = _supervisor(factory, driver)

    supervisor.tick()

    with factory() as session:
        run = session.scalars(select(Run)).one()
        spec = supervisor._spec_for(session, run)
    assert spec.env["NCCL_SOCKET_IFNAME"] == "enp86s0f0np0"
    assert spec.env["GLOO_SOCKET_IFNAME"] == "enp86s0f0np0"


def test_an_explicit_gloo_interface_is_left_alone():
    """An operator who names a different interface for Gloo means it."""
    factory = _db()
    _seed(factory, pinned_group=True, tp_values=[16])
    with factory() as session:
        session.get(MachineGroup, 1).nccl_env = {
            "NCCL_SOCKET_IFNAME": "enp86s0f0np0",
            "GLOO_SOCKET_IFNAME": "enp83s0np0",
        }
        session.commit()
    driver, _ = _driver()
    supervisor = _supervisor(factory, driver)

    supervisor.tick()

    with factory() as session:
        run = session.scalars(select(Run)).one()
        spec = supervisor._spec_for(session, run)
    assert spec.env["GLOO_SOCKET_IFNAME"] == "enp83s0np0"


def test_a_gang_waits_while_any_member_is_busy():
    """The group is the unit: one busy member means the campaign waits rather
    than half-deploying."""
    factory = _db()
    _seed(factory, pinned_group=True, tp_values=[16])
    with factory() as session:
        # A previous run on the worker box that the janitor has not confirmed
        # gone: its cards are still reserved, so the gang must wait.
        session.add(Candidate(
            id=1, campaign_id=1, config={"tp_size": 16}, config_hash="previous",
            status="exhausted",
        ))
        session.flush()
        session.add(Run(
            campaign_id=1, candidate_id=1, machine_id=2,
            status=RunStatus.SUCCEEDED.value, teardown_pending=True,
            gpu_indices=[0, 1],
        ))
        session.commit()
    driver, _ = _driver()
    supervisor = _supervisor(factory, driver)

    supervisor.tick()

    with factory() as session:
        # Only the previous run's rank-0 node; no gang was placed.
        assert [node.rank for node in session.scalars(select(RunNode))] == [0]


def test_a_group_member_still_takes_single_node_work():
    """Grouping is not a reservation: with no gang live, the same box hosts an
    ordinary single-node run."""
    factory = _db()
    _seed(factory, pinned_group=False, tp_values=[4])  # fits one 8-card box
    driver, _ = _driver()
    supervisor = _supervisor(factory, driver)

    supervisor.tick()

    with factory() as session:
        run = session.scalars(select(Run)).one()
        nodes = list(session.scalars(select(RunNode)))
        assert len(nodes) == 1  # single-node
        assert run.node_group == ""
        assert run.machine_id in (1, 2)


def test_a_config_that_does_not_divide_across_the_group_is_invalid():
    """Rejected statically, not left pending forever: a world that cannot be
    split is a launch error, and a campaign must be able to finish."""
    from app.control.search.validation import ValidationContext, validate_config

    error = validate_config(
        {"tp": 3}, ValidationContext(gpu_count=16, engine="sglang", nodes=2)
    )
    assert error is not None and "divide across 2 nodes" in error

    ok = validate_config(
        {"tp": 16}, ValidationContext(gpu_count=16, engine="sglang", nodes=2)
    )
    assert ok is None


def test_vllm_is_refused_a_group_config():
    from app.control.search.validation import ValidationContext, validate_config

    error = validate_config(
        {"tensor_parallel_size": 16},
        ValidationContext(gpu_count=16, engine="vllm", nodes=2),
    )
    assert error is not None and "not supported" in error


def test_a_lone_handle_teardown_is_unchanged():
    """The single-node path must keep behaving exactly as before, so the
    node-aware methods are inert when there is no gang."""
    driver, ssh = _driver()
    spec = LaunchSpec(
        run_id=3, machine=MachineInfo(name="node-1", host="10.0.0.1"), engine="sglang",
        image="i", model_path="/m", served_model_name="m", gpu_indices=[0],
    )
    handle, commands = driver.launch_nodes(spec)
    assert handle.node_handles == []
    assert len(commands) == 1
    assert "--nnodes" not in commands[0]
    assert driver.is_gone(handle) is False

    ssh.calls.clear()
    driver.teardown(handle)
    assert ssh.find(lambda c: c.rstrip().endswith("docker rm -f autotune-run-3")) >= 0


def test_a_finished_gang_returns_every_member_to_the_fleet():
    """A gang releases ALL its machines, not just the master: releasing only the
    master used to leave the workers reserved forever, which reads as a group
    that can never be placed again with nothing to explain it."""
    factory = _db()
    _seed(factory, pinned_group=True, tp_values=[16])
    driver, _ = _driver()
    supervisor = _supervisor(factory, driver)

    for _ in range(6):
        supervisor.tick()

    with factory() as session:
        run = session.scalars(select(Run)).one()
        assert run.status == RunStatus.SUCCEEDED.value
        assert session.get(Machine, 1).state == MachineState.AVAILABLE.value
        assert session.get(Machine, 2).state == MachineState.AVAILABLE.value


def test_a_group_member_is_released_even_when_the_master_is_not_named():
    """A gang whose master row still says RESERVED still frees the worker once
    the run is gone — the release path reads the node rows, not `run.machine`."""
    factory = _db()
    _seed(factory, pinned_group=True, tp_values=[16])
    driver, _ = _driver()
    supervisor = _supervisor(factory, driver)
    supervisor.tick()

    with factory() as session:
        run = session.scalars(select(Run)).one()
        # Finish the run as if its containers had gone quietly.
        run.status = RunStatus.SUCCEEDED.value
        run.teardown_pending = False
        supervisor._release_run_machines(session, run)
        session.commit()

    with factory() as session:
        assert session.get(Machine, 1).state == MachineState.AVAILABLE.value
        assert session.get(Machine, 2).state == MachineState.AVAILABLE.value
