"""Measuring an endpoint the platform did not launch.

A policy may serve an engine itself and ask the platform to benchmark it. The
platform launched nothing, so it cannot know when the engine is ready: it
probes, and must tell "not listening yet" (wait, as a launch waits for
readiness) from "answering badly" (fail now).
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.control.orchestrator.supervisor import Supervisor, _looks_unreachable
from app.db.base import Base
from app.db.models import (
    Campaign,
    CampaignStatus,
    Candidate,
    CandidateStatus,
    Machine,
    MachineState,
    Run,
    RunKind,
    RunStatus,
    User,
)
from app.evaluation.base import EvalOutcome, EvalStatus, Evaluator
from tests.fakes import CompletingDriver, StubEvaluator


class UnreachableEvaluator(Evaluator):
    """What the health probe returns while an engine is still loading weights."""

    name = "unreachable"

    def start(self, endpoint_url, served_model_name, context) -> str:
        return "ref"

    def poll(self, external_ref) -> EvalOutcome:
        return EvalOutcome(
            status=EvalStatus.FAILED,
            error="ConnectError: [Errno 111] Connection refused",
        )


class Sick(Evaluator):
    name = "sick"

    def start(self, endpoint_url, served_model_name, context) -> str:
        return "ref"

    def poll(self, external_ref) -> EvalOutcome:
        return EvalOutcome(status=EvalStatus.FAILED, error="/v1/models returned 500")


def _platform(health: Evaluator):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add(User(id=1, username="u", password_hash="x"))
        session.add(Machine(id=1, name="node-24", host="10.0.0.1", gpu_count=8,
                            state=MachineState.AVAILABLE.value))
        session.add(
            Campaign(id=21, owner_id=1, name="tonight", engine="sglang", image="img",
                     model_path="/m", served_model_name="m", search_space={},
                     status=CampaignStatus.ACTIVE.value, machine_names=["node-24"])
        )
        session.add(Candidate(id=1, campaign_id=21, config={"tp": 2}, config_hash="c1",
                              status=CandidateStatus.EXHAUSTED.value))
        session.add(Run(id=1, campaign_id=21, candidate_id=1, machine_id=1,
                        kind=RunKind.EXTERNAL.value, status=RunStatus.HEALTH_CHECK.value,
                        endpoint_url="http://10.0.0.1:8050", started_at=datetime.now(UTC)))
        session.commit()
    supervisor = Supervisor(
        session_factory=factory,
        health_evaluator=health,
        bench_evaluator=StubEvaluator({"score_total": 1.0}),
    )
    supervisor.driver = CompletingDriver()
    return supervisor, factory


def _run(factory) -> Run:
    with factory() as session:
        return session.get(Run, 1)


def test_an_endpoint_still_loading_is_waited_for():
    supervisor, factory = _platform(UnreachableEvaluator())
    for _ in range(5):
        supervisor.tick()
    assert _run(factory).status == RunStatus.HEALTH_CHECK.value


def test_waiting_ends_once_the_readiness_window_has_passed():
    supervisor, factory = _platform(UnreachableEvaluator())
    supervisor.tick()
    with factory() as session:  # backdate it past the readiness timeout
        session.get(Run, 1).started_at = datetime.now(UTC) - timedelta(
            minutes=supervisor.settings.ready_timeout_minutes + 1
        )
        session.commit()
    supervisor.tick()
    run = _run(factory)
    assert (run.status, run.failure_class) == (RunStatus.FAILED.value, "endpoint_unreachable")


def test_an_endpoint_that_answers_badly_fails_at_once():
    supervisor, factory = _platform(Sick())
    supervisor.tick()
    run = _run(factory)
    assert (run.status, run.failure_class) == (RunStatus.FAILED.value, "endpoint_unhealthy")


def test_unreachable_is_told_apart_from_unhealthy():
    assert _looks_unreachable("ConnectError: [Errno 111] Connection refused")
    assert _looks_unreachable("ConnectTimeout: timed out")
    assert not _looks_unreachable("/v1/models returned 500")
    assert not _looks_unreachable("empty completion (finish_reason=length)")
    assert not _looks_unreachable(None)


def test_production_is_never_measured_in_place():
    """A lease is the hand-over: the platform never inspects or benchmarks what
    was on the machine before it."""
    supervisor, factory = _platform(UnreachableEvaluator())
    supervisor.tick()
    with factory() as session:
        assert not session.scalars(select(Run).where(Run.kind == RunKind.BASELINE.value)).all()
