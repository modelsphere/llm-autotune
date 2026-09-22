"""Shared test doubles.

The default driver is the real ssh+docker one, so a Supervisor built without
an explicit driver will try to reach a fictional host and spend a minute per
call timing out. Any test that calls tick() and is not itself about
deployment wants one of these.
"""

from typing import Any

from app.control.launch.base import (
    DeploymentDriver,
    DeploymentHandle,
    DeploymentState,
    WorkloadState,
)
from app.evaluation.base import EvalOutcome, EvalStatus, Evaluator


class NullDriver(DeploymentDriver):
    """Accepts launches and stays STARTING forever.

    Runs reach LAUNCHING — so scheduling genuinely consumes capacity, which is
    what capacity-dependent assertions need — and then nothing else happens.
    """

    name = "null"

    def __init__(self):
        self.launched: list[str] = []
        self.torn_down: list[str] = []
        # Containers confirmed gone (torn down). A real container vanishes when
        # removed; the fake models that so the teardown-confirmation janitor can
        # see cleanup complete instead of a container that attaches forever.
        self._gone: set[str] = set()
        self.cleared: list[str] = []
        self.restored: list[str] = []
        # Generic workloads (policy containers): the recorded specs let a test
        # read the env a container was born with (the session token lives
        # there), and `workload_states` lets it kill one mid-test.
        self.workloads: list = []
        self.workload_states: dict[str, WorkloadState] = {}
        # Per-container stdout a test can inject, so the death-log capture path
        # has something to read (a real `docker logs` on a crashed container).
        self.workload_logs: dict[str, str] = {}

    def launch(self, spec):
        self._gone.discard(spec.container_name)  # a relaunch resurrects it
        self.launched.append(spec.container_name)
        return self._handle(spec), f"null-launch {spec.container_name}"

    def _up_state(self) -> DeploymentState:
        return DeploymentState.STARTING  # never ready, never crashed

    def state(self, handle) -> DeploymentState:
        if handle.container_name in self._gone:
            return DeploymentState.GONE
        return self._up_state()

    def logs(self, handle, tail: int = 200) -> str:
        return self.workload_logs.get(handle.container_name, "")

    def teardown(self, handle) -> None:
        self.torn_down.append(handle.container_name)
        self._gone.add(handle.container_name)
        self.workload_states.pop(handle.container_name, None)

    def attach(self, spec):
        if spec.container_name in self._gone:
            return None
        return self._handle(spec) if spec.container_name in self.launched else None

    def launch_workload(self, spec):
        self.workloads.append(spec)
        self.workload_states[spec.name] = WorkloadState.RUNNING
        handle = DeploymentHandle(
            driver=self.name, container_name=spec.name, machine=spec.machine,
            endpoint_url="",
        )
        # Env in the command, as `docker run -e K=V` would be: what makes the
        # supervisor's secret-redaction observable from a test.
        env = " ".join(f"-e {k}={v}" for k, v in sorted(spec.env.items()))
        return handle, f"null-workload {env} {spec.name}"

    def workload_state(self, handle) -> WorkloadState:
        return self.workload_states.get(handle.container_name, WorkloadState.GONE)

    def capture_baseline(self, machine) -> dict:
        return {"services": []}

    def clear_baseline(self, machine, baseline) -> list[str]:
        self.cleared.append(machine.name)
        return [s["container"] for s in (baseline or {}).get("services", [])]

    def restore_baseline(self, machine, baseline) -> list[str]:
        self.restored.append(machine.name)
        return [s["container"] for s in (baseline or {}).get("services", [])]

    @staticmethod
    def _handle(spec) -> DeploymentHandle:
        return DeploymentHandle(
            driver="null",
            container_name=spec.container_name,
            machine=spec.machine,
            endpoint_url=spec.endpoint_url,
        )


class CompletingDriver(NullDriver):
    """Reaches READY, so a run can be driven all the way to SUCCEEDED."""

    name = "completing"

    def __init__(self, environment: dict[str, Any] | None = None):
        super().__init__()
        self._env = environment or {}

    def _up_state(self) -> DeploymentState:
        return DeploymentState.READY  # gone-check still honoured via state()

    def environment(self, handle) -> dict[str, Any]:
        return dict(self._env)


class StubEvaluator(Evaluator):
    """Passes immediately with the metrics it was given."""

    name = "stub"

    def __init__(self, metrics: dict[str, Any] | None = None):
        self.metrics = metrics or {}
        self.started: list[tuple[str, str]] = []

    def start(self, endpoint_url, served_model_name, context) -> str:
        self.started.append((endpoint_url, served_model_name))
        return f"stub:{endpoint_url}"

    def poll(self, external_ref) -> EvalOutcome:
        return EvalOutcome(status=EvalStatus.PASSED, metrics=dict(self.metrics))




def seed_candidates(session, campaign, space: dict | None = None, id_start: int = 900) -> list:
    """Materialize a campaign's search space as candidate rows.

    Search lives in an external policy container, so the platform proposes
    nothing on its own: candidates normally arrive over the policy session
    API. A test about EXECUTION — launching, sharing, retrying, tearing down
    — should not have to run a policy to get something to execute, so this
    enumerates the declared space the same way `coverage` does and inserts
    the points directly. It is the platform's own expansion, not a fixture's
    idea of one.

    Ids start high so a test that also inserts candidates at explicit low ids
    does not collide with these.
    """
    from sqlalchemy import select

    from app.control.search import CandidateConfig
    from app.control.search.space import expand
    from app.db.models import Candidate, CandidateStatus

    existing = session.scalars(
        select(Candidate).where(Candidate.campaign_id == campaign.id)
    ).all()
    if existing:
        return list(existing)  # the test built its own; leave them alone

    rows = []
    for offset, config in enumerate(
        expand(space if space is not None else (campaign.search_space or {}))
    ):
        row = Candidate(
            id=id_start + offset,
            campaign_id=campaign.id,
            config=config,
            config_hash=CandidateConfig(engine_args=config).hash,
            status=CandidateStatus.VALID.value,
        )
        session.add(row)
        rows.append(row)
    session.flush()
    return rows
