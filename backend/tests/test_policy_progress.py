"""The productivity tracker (contract v1.1): explicit lifecycle signals, the
idle watchdog, and platform-derived coverage.

The heartbeat already enforced liveness (a dead or mute policy is failed toward
validation). These tests cover the other half: a policy that is alive and
beating but *done* — whether it says so, forgets to, or livelocks — and how far
into the declared space it actually got.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.control.launch.base import WorkloadState
from app.control.orchestrator.policy_lifecycle import command_for
from app.control.orchestrator.policy_session import PolicySessionEngine
from app.control.orchestrator.supervisor import Supervisor
from app.control.search import CandidateConfig
from app.control.search.coverage import space_coverage
from app.db.base import Base
from app.db.models import (
    BaselineStatus,
    Campaign,
    CampaignStatus,
    Candidate,
    Machine,
    MachineState,
    PolicySession,
    PolicySessionStatus,
    Run,
    RunKind,
    RunStatus,
    User,
)
from tests.fakes import NullDriver

GRID = {"grid": {"tp": [2, 4]}, "base": {"mem_fraction_static": 0.9}}


def _stack(*, space=None, policy_settings=None):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add(User(id=1, username="u", password_hash="x"))
        session.add(
            Machine(id=1, name="node-24", host="10.0.0.1", gpu_count=8,
                    state=MachineState.AVAILABLE.value,
                    baseline_status=BaselineStatus.CLEARED.value)
        )
        session.add(
            Campaign(id=1, owner_id=1, name="c", engine="sglang", image="img",
                     model_path="/m", served_model_name="m",
                     search_space=space or GRID,
                     objective={"target_metric": "x"},
                     benchmark_slug="autotune-test-v0",
                     status=CampaignStatus.ACTIVE.value,
                     window_end=datetime.now(UTC) + timedelta(hours=6),
                     policy_settings=policy_settings or {})
        )
        session.commit()
    supervisor = Supervisor(session_factory=factory)
    supervisor.driver = NullDriver()
    return supervisor, factory


def _engine(supervisor):
    """A session engine whose workload always looks alive, so the tests exercise
    the signal/idle logic rather than the container-death path."""
    eng = PolicySessionEngine(supervisor)
    eng._workload_state = lambda *a, **k: WorkloadState.RUNNING
    return eng


def _searching(session, **overrides):
    row = PolicySession(
        id=1, campaign_id=1, policy_id=1, machine_id=1,
        status=PolicySessionStatus.SEARCHING.value,
        container_name="pol-1", started_at=datetime.now(UTC) - timedelta(minutes=30),
        last_heartbeat_at=datetime.now(UTC),
        **overrides,
    )
    session.add(row)
    session.flush()
    return row


# -- explicit signals ----------------------------------------------------------


def test_exhausted_signal_finalizes_promptly():
    supervisor, factory = _stack()
    eng = _engine(supervisor)
    with factory() as db:
        sess = _searching(db, policy_status="exhausted")
        campaign = db.get(Campaign, 1)
        machine = db.get(Machine, 1)
        eng._advance_searching(db, sess, campaign, machine)
        assert sess.status == PolicySessionStatus.FINALIZING.value
        assert sess.search_end_reason == "policy_exhausted"


def test_error_signal_finalizes_and_records_the_failure():
    supervisor, factory = _stack()
    eng = _engine(supervisor)
    with factory() as db:
        sess = _searching(db, policy_status="error", failure_class="tuner_crashed")
        eng._advance_searching(db, sess, db.get(Campaign, 1), db.get(Machine, 1))
        assert sess.status == PolicySessionStatus.FINALIZING.value
        assert sess.search_end_reason == "policy_error"
        assert sess.failure_class == "tuner_crashed"  # the policy's own class wins


def test_command_for_tells_a_done_policy_to_finalize_this_beat():
    """The API derives the command; a policy that just signalled exhausted is
    told to finalize on the same heartbeat, before the worker's next tick."""
    supervisor, factory = _stack()
    with factory() as db:
        sess = _searching(db, policy_status="exhausted")
        out = command_for(sess, db.get(Campaign, 1), db.get(Machine, 1), pending_contenders=0)
        assert out["command"] == "finalize"


# -- the idle watchdog ---------------------------------------------------------


def test_idle_watchdog_strikes_then_finalizes():
    """Alive and beating but doing no delegated work: a strike per idle window,
    and enough consecutive strikes ends the search — the enforceable backstop
    for a policy that never sends 'exhausted'."""
    supervisor, factory = _stack(
        policy_settings={"search_idle_timeout_s": 60, "search_idle_strikes": 3}
    )
    eng = _engine(supervisor)
    with factory() as db:
        # Idle for well over one window, and never any delegated activity.
        sess = _searching(db, last_activity_at=datetime.now(UTC) - timedelta(minutes=10))
        campaign, machine = db.get(Campaign, 1), db.get(Machine, 1)

        eng._advance_searching(db, sess, campaign, machine)
        assert sess.idle_strikes == 1
        assert sess.status == PolicySessionStatus.SEARCHING.value

        # Simulate the next window elapsing (the strike is rate-limited to once
        # per timeout, keyed off last_idle_strike_at).
        sess.last_idle_strike_at = datetime.now(UTC) - timedelta(minutes=2)
        eng._advance_searching(db, sess, campaign, machine)
        assert sess.idle_strikes == 2
        assert sess.status == PolicySessionStatus.SEARCHING.value

        sess.last_idle_strike_at = datetime.now(UTC) - timedelta(minutes=2)
        eng._advance_searching(db, sess, campaign, machine)
        assert sess.idle_strikes == 3
        assert sess.status == PolicySessionStatus.FINALIZING.value
        assert sess.search_end_reason == "policy_idle"


def test_a_strike_is_rate_limited_to_one_per_window():
    """Two ticks inside the same idle window must not burn two strikes."""
    supervisor, factory = _stack(
        policy_settings={"search_idle_timeout_s": 60, "search_idle_strikes": 3}
    )
    eng = _engine(supervisor)
    with factory() as db:
        sess = _searching(db, last_activity_at=datetime.now(UTC) - timedelta(minutes=10))
        campaign, machine = db.get(Campaign, 1), db.get(Machine, 1)
        eng._advance_searching(db, sess, campaign, machine)
        eng._advance_searching(db, sess, campaign, machine)  # same window
        assert sess.idle_strikes == 1


def test_an_in_flight_run_never_trips_the_watchdog():
    """A long benchmark still running is productive by definition; the idle
    clock must not accrue against it even though no new request is being made."""
    supervisor, factory = _stack(
        policy_settings={"search_idle_timeout_s": 60, "search_idle_strikes": 1}
    )
    eng = _engine(supervisor)
    with factory() as db:
        sess = _searching(db, last_activity_at=datetime.now(UTC) - timedelta(minutes=10))
        cand = Candidate(id=1, campaign_id=1, config={"tp": 2},
                         config_hash=CandidateConfig(engine_args={"tp": 2}).hash)
        db.add(cand)
        db.flush()
        db.add(Run(id=1, campaign_id=1, candidate_id=1, machine_id=1,
                   policy_session_id=1, kind=RunKind.EXTERNAL.value,
                   status=RunStatus.BENCHING.value))
        db.flush()
        eng._advance_searching(db, sess, db.get(Campaign, 1), db.get(Machine, 1))
        assert sess.idle_strikes == 0
        assert sess.status == PolicySessionStatus.SEARCHING.value


# -- platform-derived coverage -------------------------------------------------


def _trial(config):
    canonical = {"mem_fraction_static": 0.9, **config}
    return (CandidateConfig(engine_args=canonical).hash, canonical)


def test_coverage_counts_cells_from_the_ledger_not_the_policy():
    """Grid tp∈{2,4}: measuring tp=2 covers one of two cells, and the untried
    cell is named exactly — computed from the trials, not anything reported."""
    cov = space_coverage(GRID, [_trial({"tp": 2})])
    assert cov.enumerable
    assert cov.cells_total == 2
    assert cov.cells_covered == 1
    assert cov.remaining == [{"tp": 4}]
    tp_axis = next(a for a in cov.axes if a.param == "tp")
    assert tp_axis.covered == 1 and tp_axis.total == 2
    hit = {v.value: v.covered for v in tp_axis.values}
    assert hit == {2: True, 4: False}


def test_coverage_flags_trials_outside_the_declared_space():
    cov = space_coverage(GRID, [_trial({"tp": 2}), _trial({"tp": 8})])
    assert cov.cells_covered == 1  # tp=8 is not a declared cell
    assert cov.off_space == 1


def test_coverage_echoes_the_policy_self_report():
    declared = {"fraction": 0.5, "note": "half the interesting region",
                "dimensions": [{"param": "tp", "skipped_values": [4]}]}
    cov = space_coverage(GRID, [_trial({"tp": 2})], declared)
    assert cov.policy_declared is not None
    assert cov.policy_declared.fraction == 0.5
    assert cov.policy_declared.dimensions[0].skipped_values == [4]
