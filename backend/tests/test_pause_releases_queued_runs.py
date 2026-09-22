"""Pausing a campaign gives back its place in the cluster's queue.

An unplaced run on a shared GPU cluster is pure demand: a pod in the
scheduler's queue, competing with production for the next card that frees.
Pause is how a human says "prod needs the cluster back", so holding that slot
is the one thing a paused campaign must not keep doing. Withdrawing costs
nothing — an unplaced pod has done no work — and the candidate is submitted
again on resume.

What pause must NOT do is disturb a run that already holds hardware, or a
machine with no queue to leave at all (ssh_docker: "pause means frozen",
node-85 2026-08-19).
"""

from sqlalchemy import select

from app.control.launch.base import LaunchSpec
from app.control.launch.failures import RELEASED
from app.db.models import (
    Campaign,
    CampaignStatus,
    Candidate,
    CandidateStatus,
    Event,
    Run,
    RunStatus,
)
from tests.test_supervisor import FakeDriver, make_supervisor


class QueuedDriver(FakeDriver):
    """A cluster substrate that has not placed the run yet."""

    def __init__(self, placement: str = "queued"):
        super().__init__()
        self._placement = placement

    def placement(self, handle) -> str:
        return self._placement

    def attach(self, spec: LaunchSpec):
        handle, _ = self.launch(spec)
        return handle


def _paused_campaign_with_a_run(placement: str, status=RunStatus.LAUNCHING):
    supervisor, factory = make_supervisor()
    supervisor.driver = QueuedDriver(placement=placement)
    with factory() as session:
        session.add(Candidate(id=1, campaign_id=1, config={"tp_size": 2},
                              config_hash="h1",
                              status=CandidateStatus.EXHAUSTED.value))
        session.flush()
        session.add(Run(id=1, campaign_id=1, candidate_id=1, machine_id=1,
                        status=status.value, container_name="autotune-run-1",
                        gpu_indices=[0, 1]))
        session.get(Campaign, 1).status = CampaignStatus.PAUSED.value
        session.commit()
    return supervisor, factory


def test_a_queued_run_is_withdrawn_when_the_campaign_is_paused():
    supervisor, factory = _paused_campaign_with_a_run("queued")
    with factory() as session:
        supervisor._release_queued_while_paused(session)
        session.commit()
    with factory() as session:
        run = session.get(Run, 1)
        assert run.status == RunStatus.KILLED.value
        assert run.failure_class == RELEASED
        assert "paused" in run.error
        # The candidate is back in the pool, ready to be submitted on resume.
        assert session.get(Candidate, 1).status == CandidateStatus.VALID.value
        kinds = {e.kind for e in session.scalars(select(Event)).all()}
        assert "run_released_to_queue" in kinds


def test_a_placed_run_is_left_alone():
    """Already bound to a node: it is not competing for cards, it won them.
    Killing it would throw away a model load that may be nearly ready."""
    supervisor, factory = _paused_campaign_with_a_run("placed")
    with factory() as session:
        supervisor._release_queued_while_paused(session)
        session.commit()
    with factory() as session:
        assert session.get(Run, 1).status == RunStatus.LAUNCHING.value


def test_an_unreadable_substrate_is_left_alone():
    """"unknown" must never be treated as "queued" — withdrawing on a failed
    read would kill live work."""
    supervisor, factory = _paused_campaign_with_a_run("unknown")
    with factory() as session:
        supervisor._release_queued_while_paused(session)
        session.commit()
    with factory() as session:
        assert session.get(Run, 1).status == RunStatus.LAUNCHING.value


def test_withdrawn_runs_do_not_spend_the_retry_budget():
    """MAX_ATTEMPTS_PER_CANDIDATE exists to stop an unlaunchable config looping.
    A run WE took back tried nothing and learned nothing, so pausing three
    times must not exhaust a candidate that has never actually run."""
    supervisor, factory = _paused_campaign_with_a_run("queued")
    with factory() as session:
        # Three past withdrawals, then one genuine infrastructure failure.
        for run_id in (2, 3, 4):
            session.add(Run(id=run_id, campaign_id=1, candidate_id=1, machine_id=1,
                            status=RunStatus.KILLED.value, failure_class=RELEASED))
        failed = Run(id=5, campaign_id=1, candidate_id=1, machine_id=1,
                     status=RunStatus.FAILED.value, failure_class="unschedulable")
        session.add(failed)
        session.commit()
    with factory() as session:
        run = session.get(Run, 5)
        run.candidate.status = CandidateStatus.EXHAUSTED.value
        supervisor._requeue_if_infrastructure(session, run)
        session.commit()
    with factory() as session:
        assert session.get(Candidate, 1).status == CandidateStatus.VALID.value
        kinds = [e.kind for e in session.scalars(select(Event)).all()]
        assert "candidate_requeued" in kinds
        assert "candidate_given_up" not in kinds


def test_a_substrate_with_no_queue_is_never_even_asked():
    """ssh_docker has no queue to leave, and `attach()` there is an ssh
    round-trip. A paused campaign must not pay one per run per tick to be told
    the same thing forever, so a driver that never overrode placement() is
    skipped without touching the machine at all."""
    supervisor, factory = _paused_campaign_with_a_run("queued")
    plain = FakeDriver()  # inherits the base placement() → cannot queue
    supervisor.driver = plain
    with factory() as session:
        supervisor._release_queued_while_paused(session)
        session.commit()
    with factory() as session:
        assert session.get(Run, 1).status == RunStatus.LAUNCHING.value
    assert plain.launched == [], "the machine was contacted for a substrate with no queue"
