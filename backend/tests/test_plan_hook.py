"""Planning a campaign in a plugin, and the history a planner reads.

A plugin that plans a campaign replaces the default enumeration of its space;
what it proposes is still validated and deduplicated by the platform. A
planner that fails costs its campaign one tick of new candidates and nothing
else: falling back to enumeration would fill a campaign meant to be searched
cleverly with every point of its space.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import plugins
from app.control.orchestrator.supervisor import Supervisor
from app.control.search import CandidateConfig
from app.control.search.history import campaign_history
from app.db.base import Base
from app.db.models import (
    BaselineStatus,
    Campaign,
    CampaignStatus,
    Candidate,
    CandidateStatus,
    Machine,
    MachineState,
    Result,
    Run,
    RunStatus,
    User,
)
from app.plugins import PLUGIN_API_VERSION, Plugin
from tests.fakes import NullDriver

SLO = {"target_metric": "tpm_card", "redlines": [{"metric": "ttft", "op": "<=", "value": 7000}]}


def _stack(search_space=None):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add(User(id=1, username="u", password_hash="x"))
        session.add(
            Machine(
                id=1, name="gpu-01", host="10.0.0.1", gpu_count=8,
                state=MachineState.AVAILABLE.value,
                baseline_status=BaselineStatus.CLEARED.value,
            )
        )
        session.add(
            Campaign(
                id=1, owner_id=1, name="c", engine="sglang", image="img",
                model_path="/m", served_model_name="m",
                search_space={"grid": {"tp": [1, 2, 4]}} if search_space is None else search_space,
                objective=SLO,
                status=CampaignStatus.ACTIVE.value,
                run_baseline_canary=False,
            )
        )
        session.commit()
    supervisor = Supervisor(session_factory=factory)
    supervisor.driver = NullDriver()
    return supervisor, factory


def _plan(supervisor):
    """One planning step, as the tick runs it."""
    with supervisor.session_factory() as session:
        supervisor._plan(session)
        session.commit()


def _configs(factory) -> list[tuple[dict, str]]:
    with factory() as session:
        rows = session.scalars(select(Candidate).order_by(Candidate.id)).all()
        return [(c.config, c.status) for c in rows]


def _with(monkeypatch, *planners):
    enabled = tuple(
        Plugin(name=f"p{i}", api_version=PLUGIN_API_VERSION, propose_candidates=planner)
        for i, planner in enumerate(planners)
    )
    monkeypatch.setattr(plugins, "enabled", lambda: enabled)


# ------------------------------------------------------------------ the hook


def test_without_a_planner_the_space_is_enumerated(monkeypatch):
    supervisor, factory = _stack()
    _with(monkeypatch, lambda ctx: None)

    _plan(supervisor)

    assert [c for c, _ in _configs(factory)] == [{"tp": 1}, {"tp": 2}, {"tp": 4}]


def test_a_plugin_that_plans_a_campaign_replaces_enumeration(monkeypatch):
    supervisor, factory = _stack()
    _with(monkeypatch, lambda ctx: [CandidateConfig({"tp": 4}), CandidateConfig({"tp": 2})])

    _plan(supervisor)

    assert [c for c, _ in _configs(factory)] == [{"tp": 4}, {"tp": 2}]


def test_what_a_planner_proposes_is_validated_and_deduplicated(monkeypatch):
    supervisor, factory = _stack()
    _with(
        monkeypatch,
        lambda ctx: [
            CandidateConfig({"tp": 2}),
            CandidateConfig({"tp": 2}),  # the same point twice in one answer
            CandidateConfig({"tp": 16}),  # more cards than the machine has
        ],
    )

    _plan(supervisor)
    _plan(supervisor)  # and again next tick

    assert _configs(factory) == [
        ({"tp": 2}, CandidateStatus.VALID.value),
        ({"tp": 16}, CandidateStatus.INVALID.value),
    ]


def test_a_planner_plans_even_a_campaign_with_no_declared_space(monkeypatch):
    supervisor, factory = _stack(search_space={})
    _with(monkeypatch, lambda ctx: [CandidateConfig({"tp": 1})])

    _plan(supervisor)

    assert [c for c, _ in _configs(factory)] == [{"tp": 1}]


def test_the_first_plugin_to_answer_plans_it(monkeypatch):
    supervisor, factory = _stack()
    _with(
        monkeypatch,
        lambda ctx: None,
        lambda ctx: [CandidateConfig({"tp": 1})],
        lambda ctx: [CandidateConfig({"tp": 2})],
    )

    _plan(supervisor)

    assert [c for c, _ in _configs(factory)] == [{"tp": 1}]


def test_a_planner_that_fails_costs_its_campaign_one_tick_not_an_enumeration(
    monkeypatch, caplog
):
    supervisor, factory = _stack()

    def broken(ctx):
        raise RuntimeError("model would not fit")

    _with(monkeypatch, broken)

    _plan(supervisor)

    assert _configs(factory) == []
    assert "planning campaign 1 failed" in caplog.text


def test_the_context_says_what_could_start_and_what_is_queued(monkeypatch):
    supervisor, _ = _stack()
    seen = {}

    def planner(ctx):
        seen.update(slots=ctx.startable_slots(), queued=ctx.queued(), cap=ctx.batch_cap)
        seen["objective"] = ctx.campaign.objective
        return []

    _with(monkeypatch, planner)

    _plan(supervisor)

    # Eight free cards and nothing queued: at most eight runs could start now.
    assert seen == {"slots": 8, "queued": 0, "cap": 100, "objective": SLO}


# ------------------------------------------------------------------ history


def test_history_carries_the_resolved_objective_not_just_raw_metrics():
    """A planner must not have to know which of ~40 metric keys the campaign
    optimizes, nor how a redline is spelled."""
    _, factory = _stack()
    now = datetime.now(UTC)
    with factory() as session:
        session.add(Candidate(id=1, campaign_id=1, config={"tp": 2}, config_hash="h1",
                              status=CandidateStatus.EXHAUSTED.value))
        session.add(Run(id=1, campaign_id=1, candidate_id=1, machine_id=1,
                        status=RunStatus.SUCCEEDED.value,
                        started_at=now - timedelta(minutes=30), finished_at=now))
        session.add(Result(run_id=1, source="llmbench", passed=True,
                           metrics={"tpm_card": 50000.0, "ttft": 5000.0}))
        session.commit()

        history = campaign_history(session, session.get(Campaign, 1))

    record = next(r for r in history if r.config == {"tp": 2})
    assert record.objective_value == 50000.0
    assert record.feasible
    assert record.constraints == (5000.0 - 7000,), "slack, satisfied when <= 0"
    assert record.duration_seconds == pytest.approx(1800, abs=2)
    assert not record.in_flight


def test_a_config_failure_reaches_the_planner_as_one():
    """An OOM is an observation to learn from; an ssh timeout is not."""
    _, factory = _stack()
    with factory() as session:
        for cid, failure in ((1, "oom"), (2, "ssh_timeout")):
            session.add(Candidate(id=cid, campaign_id=1, config={"tp": cid},
                                  config_hash=f"h{cid}",
                                  status=CandidateStatus.EXHAUSTED.value))
            session.add(Run(id=cid, campaign_id=1, candidate_id=cid, machine_id=1,
                            status=RunStatus.FAILED.value, failure_class=failure))
        session.commit()

        history = campaign_history(session, session.get(Campaign, 1))

    by_config = {r.config["tp"]: r for r in history if "tp" in r.config}
    assert by_config[1].config_induced_failure
    assert not by_config[1].feasible
    assert not by_config[2].config_induced_failure


def test_the_baseline_canary_is_not_offered_as_a_search_point():
    _, factory = _stack()
    with factory() as session:
        session.add(Candidate(id=1, campaign_id=1, config={"__baseline__": "prod-svc"},
                              config_hash="baseline-1",
                              status=CandidateStatus.EXHAUSTED.value))
        session.add(Run(id=1, campaign_id=1, candidate_id=1, machine_id=1,
                        kind="baseline", status=RunStatus.SUCCEEDED.value))
        session.commit()

        history = campaign_history(session, session.get(Campaign, 1))

    assert not any("__baseline__" in r.config for r in history)


def test_queued_and_rejected_candidates_count_as_tried():
    _, factory = _stack()
    with factory() as session:
        session.add(Candidate(id=1, campaign_id=1, config={"tp": 2}, config_hash="q",
                              status=CandidateStatus.VALID.value))
        session.add(Candidate(id=2, campaign_id=1, config={"tp": 16}, config_hash="r",
                              status=CandidateStatus.INVALID.value))
        session.commit()

        history = {r.config["tp"]: r for r in campaign_history(session, session.get(Campaign, 1))}

    assert history[2].in_flight and history[2].status == "candidate_valid"
    assert history[16].config_induced_failure and not history[16].in_flight
