"""The hand-over lifecycle, from the two ways it went wrong on node-24.

Both failures came from the same place: a rule that was right in the moment it
was written, applied to evidence that had gone stale or arrived too early.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.control.orchestrator.supervisor import Supervisor, _looks_unreachable
from app.db.base import Base
from app.db.models import (
    BaselineStatus,
    Campaign,
    CampaignStatus,
    Candidate,
    CandidateKind,
    Event,
    Machine,
    MachineState,
    Run,
    RunKind,
    RunStatus,
    User,
)
from app.evaluation.base import EvalOutcome, EvalStatus, Evaluator
from tests.fakes import CompletingDriver, StubEvaluator

BASELINE = {
    "services": [
        {
            "container": "sglang-modelforge-0.2-p8050",
            "endpoint_url": "http://10.0.0.1:8050",
            "served_model_name": "glm-5",
            "port": "8050",
            "cards": 2,
            # Production's real engine config, as capture now stores it.
            "engine_args": {
                "tp": "2",
                "chunked_prefill_size": "32768",
                "enable_cache_report": True,
            },
        }
    ]
}


class UnreachableEvaluator(Evaluator):
    """What the health probe returns while a restored service loads weights."""

    name = "unreachable"

    def start(self, endpoint_url, served_model_name, context) -> str:
        return "ref"

    def poll(self, external_ref) -> EvalOutcome:
        return EvalOutcome(
            status=EvalStatus.FAILED,
            error="ConnectError: [Errno 111] Connection refused",
        )


def _platform(*, window_end=None, machines_named=None, cleared_event_at=None,
              old_campaign_window=None, auto_restore=True):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add(User(id=1, username="u", password_hash="x"))
        session.add(
            Machine(id=1, name="node-24", host="10.0.0.1", gpu_count=8,
                    state=MachineState.AVAILABLE.value,
                    baseline=BASELINE,
                    baseline_status=BaselineStatus.CLEARED.value)
        )
        if old_campaign_window is not None:
            # A campaign that finished days ago, pinned to this machine, whose
            # window is long expired.
            session.add(
                Campaign(id=17, owner_id=1, name="last week", engine="sglang",
                         image="img", model_path="/m", served_model_name="m",
                         search_space={}, status=CampaignStatus.DONE.value,
                         machine_names=["node-24"], window_end=old_campaign_window)
            )
        session.add(
            Campaign(id=21, owner_id=1, name="tonight", engine="sglang", image="img",
                     model_path="/m", served_model_name="m",
                     search_space={"grid": {"tp": [2]}},
                     status=CampaignStatus.ACTIVE.value,
                     machine_names=machines_named or [], window_end=window_end)
        )
        if cleared_event_at is not None:
            session.add(Event(actor="admin", kind="baseline_cleared",
                              payload={"machine": "node-24"}, ts=cleared_event_at))
        session.commit()
    supervisor = Supervisor(
        session_factory=factory,
        health_evaluator=StubEvaluator({"probe_output_chars": 2}),
        bench_evaluator=StubEvaluator({"score_total": 1.0}),
    )
    supervisor.driver = CompletingDriver()
    # These tests exercise the put-back timing, which is opt-in; default it on
    # here so each case reads restore behaviour, and let a case pass False to
    # assert the default (admin-owned restore).
    supervisor.settings = supervisor.settings.model_copy(
        update={"auto_restore_production": auto_restore}
    )
    return supervisor, factory


def _status(factory) -> str:
    with factory() as session:
        return session.get(Machine, 1).baseline_status


# -- a finished campaign must not undo a fresh clear --------------------------


def test_an_expired_window_from_a_finished_campaign_does_not_restore_production():
    """node-24, 2026-08-03: an operator cleared the machine by hand and the worker
    put production back eight seconds later, on the orders of a campaign that
    had been done for four days. Its hand-back was honoured long ago; the
    expired window is not a standing instruction."""
    now = datetime.now(UTC)
    supervisor, factory = _platform(
        old_campaign_window=now - timedelta(days=4),
        cleared_event_at=now - timedelta(seconds=8),
    )

    supervisor.tick()

    assert _status(factory) == BaselineStatus.CLEARED.value
    with factory() as session:
        assert not session.scalars(
            select(Event).where(Event.kind.like("baseline_restored%"))
        ).all()


def test_a_window_that_closed_after_the_clear_still_restores():
    """The case the rule exists for: tonight's campaign said hand it back by
    06:00, the machine was cleared at 22:00, and 06:00 has passed."""
    now = datetime.now(UTC)
    supervisor, factory = _platform(
        window_end=now - timedelta(minutes=5),
        machines_named=["node-24"],
        cleared_event_at=now - timedelta(hours=8),
    )

    supervisor.tick()

    assert _status(factory) == BaselineStatus.RESTORED.value


def test_with_no_clear_recorded_the_window_still_governs():
    """Machines cleared before the audit trail existed must not become
    un-restorable."""
    now = datetime.now(UTC)
    supervisor, factory = _platform(
        window_end=now - timedelta(minutes=5), machines_named=["node-24"]
    )

    supervisor.tick()

    assert _status(factory) == BaselineStatus.RESTORED.value


# -- a canary must wait for a service that is still loading -------------------


def test_a_canary_waits_for_a_service_that_is_still_loading():
    """Restore returns in seconds; a 35B model loads in minutes. Probing
    immediately found a refused connection, and because a canary resolves
    inside one tick, all three attempts burned in twenty seconds against a
    service that was about to be fine."""
    supervisor, factory = _platform(machines_named=["node-24"])
    supervisor.health = UnreachableEvaluator()
    with factory() as session:
        session.get(Machine, 1).baseline_status = BaselineStatus.CAPTURED.value
        session.commit()

    for _ in range(5):
        supervisor.tick()

    with factory() as session:
        canaries = session.scalars(
            select(Run).where(Run.kind == RunKind.BASELINE.value)
        ).all()
        assert len(canaries) == 1, "one attempt, still waiting — not three burned"
        assert canaries[0].status == RunStatus.HEALTH_CHECK.value


def test_a_canary_carries_productions_config_the_way_a_search_config_is_carried():
    """The unification: a canary's candidate holds production's real engine
    args, marked by kind=baseline — not a sentinel naming a container. So it
    reads, card-normalizes and compares exactly like a swept config, and nothing
    that would launch it is fooled by a magic key inside the config."""
    supervisor, factory = _platform(machines_named=["node-24"])
    with factory() as session:
        session.get(Machine, 1).baseline_status = BaselineStatus.CAPTURED.value
        session.commit()

    supervisor.tick()

    with factory() as session:
        canary = session.scalars(
            select(Run).where(Run.kind == RunKind.BASELINE.value)
        ).one()
        candidate = session.get(Candidate, canary.candidate_id)
        assert candidate.kind == CandidateKind.BASELINE.value
        assert candidate.config == BASELINE["services"][0]["engine_args"]
        assert "__baseline__" not in candidate.config


def test_a_canary_gives_up_once_the_readiness_window_has_passed():
    supervisor, factory = _platform(machines_named=["node-24"])
    supervisor.health = UnreachableEvaluator()
    with factory() as session:
        session.get(Machine, 1).baseline_status = BaselineStatus.CAPTURED.value
        session.commit()

    supervisor.tick()
    with factory() as session:  # backdate it past the readiness timeout
        run = session.scalars(select(Run)).one()
        run.started_at = datetime.now(UTC) - timedelta(
            minutes=supervisor.settings.ready_timeout_minutes + 1
        )
        session.commit()
    supervisor.tick()

    with factory() as session:
        run = session.scalars(select(Run)).one()
        assert run.status == RunStatus.FAILED.value
        assert run.failure_class == "baseline_unreachable"
    assert supervisor.driver.cleared == [], "production survives a failed canary"


def test_a_service_that_answers_badly_is_still_condemned_immediately():
    """Waiting is only for silence. A service that replies with garbage is
    unhealthy now, and no amount of waiting changes that."""
    supervisor, factory = _platform(machines_named=["node-24"])

    class Sick(Evaluator):
        name = "sick"

        def start(self, endpoint_url, served_model_name, context) -> str:
            return "ref"

        def poll(self, external_ref) -> EvalOutcome:
            return EvalOutcome(status=EvalStatus.FAILED,
                               error="/v1/models returned 500")

    supervisor.health = Sick()
    with factory() as session:
        session.get(Machine, 1).baseline_status = BaselineStatus.CAPTURED.value
        session.commit()

    supervisor.tick()

    with factory() as session:
        run = session.scalars(select(Run)).one()
        assert run.status == RunStatus.FAILED.value
        assert run.failure_class == "baseline_unhealthy"


def test_unreachable_is_told_apart_from_unhealthy():
    assert _looks_unreachable("ConnectError: [Errno 111] Connection refused")
    assert _looks_unreachable("ConnectTimeout: timed out")
    assert not _looks_unreachable("/v1/models returned 500")
    assert not _looks_unreachable("empty completion (finish_reason=length)")
    assert not _looks_unreachable(None)
