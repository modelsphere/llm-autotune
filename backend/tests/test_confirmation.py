"""Top-K confirmation: re-run the best candidates before crowning one.

The failure this exists for is on record. On node-24, tp=4 @ mem 0.90 regressed a
functional check (0.980 vs 1.000) while performing identically to tp=4 @ mem
0.85 — and nobody knew whether it reproduced, because every config was measured
exactly once. A single sample cannot tell "fastest" from "fastest that night".
"""

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.control.orchestrator.supervisor import Supervisor
from app.control.search import CandidateConfig
from app.db.base import Base
from app.db.models import (
    BaselineStatus,
    Campaign,
    CampaignStatus,
    Candidate,
    CandidateKind,
    CandidateStatus,
    Machine,
    MachineState,
    Result,
    Run,
    RunStatus,
    User,
)
from app.reporting import render_campaign_report
from tests.fakes import NullDriver

SLO = {
    "target_metric": "tpm_card",
    "redlines": [{"metric": "pass_rate", "op": ">=", "value": 0.99}],
}
TWO_POINTS = {"grid": {"tp": [2, 4]}}


def _stack(confirm_top_k=1, confirm_repeats=3, space=None):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add(User(id=1, username="u", password_hash="x"))
        session.add(
            Machine(id=1, name="gpu-01", host="10.0.0.1", gpu_count=8,
                    state=MachineState.AVAILABLE.value,
                    baseline_status=BaselineStatus.CLEARED.value)
        )
        session.add(
            Campaign(id=1, owner_id=1, name="c", engine="sglang", image="img",
                     model_path="/m", served_model_name="m",
                     search_space=space or {"grid": {"tp": [2]}},
                     objective=SLO, status=CampaignStatus.ACTIVE.value,
                     run_baseline_canary=False,
                     confirm_top_k=confirm_top_k, confirm_repeats=confirm_repeats)
        )
        session.commit()
    supervisor = Supervisor(session_factory=factory)
    supervisor.driver = NullDriver()
    return supervisor, factory


def _measured(session, run_id, candidate_id, config, value, pass_rate=1.0,
              status=RunStatus.SUCCEEDED.value):
    """A finished run with a benchmark result already summarized, the way
    _record_result leaves it.

    The config hash is the real one the planner computes, so a measured point
    reads as already-explored and the grid does not re-propose it."""
    session.add(Candidate(id=candidate_id, campaign_id=1, config=config,
                          config_hash=CandidateConfig(engine_args=config).hash,
                          status=CandidateStatus.EXHAUSTED.value))
    session.add(Run(id=run_id, campaign_id=1, candidate_id=candidate_id,
                    machine_id=1, status=status))
    session.add(Result(run_id=run_id, source="llmbench", passed=True,
                       metrics={"tpm_card": value, "pass_rate": pass_rate},
                       objective_value=value,
                       feasible=pass_rate >= 0.99,
                       breaches=[] if pass_rate >= 0.99 else ["pass_rate below 0.99"]))


def _candidates(factory, kind=CandidateKind.CONFIRMATION.value):
    with factory() as session:
        return session.scalars(
            select(Candidate).where(Candidate.kind == kind).order_by(Candidate.id)
        ).all()


def test_the_leader_is_queued_for_repeats_before_the_campaign_finishes():
    supervisor, factory = _stack(space=TWO_POINTS, confirm_top_k=1, confirm_repeats=3)
    with factory() as session:
        _measured(session, 1, 1, {"tp": 2}, 68000.0)
        _measured(session, 2, 2, {"tp": 4}, 43000.0)
        session.commit()

    supervisor.tick()

    repeats = _candidates(factory)
    assert len(repeats) == 2, "one measurement exists, two more make three"
    assert all(c.config == {"tp": 2} for c in repeats), "only the leader"
    assert all(c.status == CandidateStatus.VALID.value for c in repeats)
    with factory() as session:
        assert session.get(Campaign, 1).status == CampaignStatus.ACTIVE.value


def test_a_repeat_keeps_the_hash_of_the_point_it_repeats():
    """That hash is how a planner knows a config was tried. A repeat that
    looked like a new point would make the search think it had explored more
    of the space than it had."""
    supervisor, factory = _stack(confirm_top_k=1, confirm_repeats=2)
    with factory() as session:
        _measured(session, 1, 1, {"tp": 2}, 68000.0)
        session.commit()

    supervisor.tick()

    repeat = _candidates(factory)[0]
    assert repeat.config_hash == CandidateConfig(engine_args={"tp": 2}).hash
    assert repeat.repeat_of == 1


def test_confirmation_is_bounded_by_attempts_not_successes():
    """Otherwise a config whose repeats keep failing is queued forever and the
    campaign never finishes."""
    supervisor, factory = _stack(confirm_top_k=1, confirm_repeats=3)
    with factory() as session:
        _measured(session, 1, 1, {"tp": 2}, 68000.0)
        # Two further attempts at the same config that produced nothing.
        for run_id, cand_id in ((2, 2), (3, 3)):
            session.add(Candidate(id=cand_id, campaign_id=1, config={"tp": 2},
                                  config_hash=CandidateConfig(
                                      engine_args={"tp": 2}).hash,
                                  status=CandidateStatus.EXHAUSTED.value,
                                  kind=CandidateKind.CONFIRMATION.value, repeat_of=1))
            session.add(Run(id=run_id, campaign_id=1, candidate_id=cand_id,
                            machine_id=1, status=RunStatus.FAILED.value,
                            failure_class="oom"))
        session.commit()

    supervisor.tick()

    assert len(_candidates(factory)) == 2, "no fourth attempt"
    with factory() as session:
        assert session.get(Campaign, 1).status == CampaignStatus.DONE.value


def test_a_baseline_candidate_is_never_launched_as_an_experiment():
    """A canary rides on a Candidate row so it can reuse the run machinery, but
    its config names a production container. "Retry failed" used to re-queue a
    failed canary into the experiment queue, where the scheduler would render
    `--__baseline__ <container>` and the engine would reject it — after a full
    model load, three times over.

    Retired rather than skipped: a candidate left VALID forever also keeps the
    campaign out of DONE.
    """
    supervisor, factory = _stack(space=TWO_POINTS, confirm_top_k=0)
    with factory() as session:
        session.add(Candidate(id=99, campaign_id=1,
                              config={"__baseline__": "prod-svc-p8050"},
                              config_hash="baseline-1",
                              status=CandidateStatus.VALID.value))
        session.commit()

    supervisor.tick()

    with factory() as session:
        assert session.get(Candidate, 99).status == CandidateStatus.EXHAUSTED.value
        launched = [r for r in session.scalars(select(Run)).all()
                    if r.candidate_id == 99]
        assert not launched, "it must never become a run"


def test_the_search_is_exhausted_before_anything_is_confirmed():
    """Confirmation spends machine-hours on points already measured, so it must
    not start while there is unexplored space left. Otherwise a campaign would
    re-run its early leader all night and never reach the configs that might
    have beaten it."""
    supervisor, factory = _stack(space=TWO_POINTS, confirm_top_k=1, confirm_repeats=3)
    with factory() as session:
        _measured(session, 1, 1, {"tp": 2}, 68000.0)  # tp=4 never tried
        session.commit()

    supervisor.tick()

    assert not _candidates(factory), "tp=4 is still unexplored"
    with factory() as session:
        untried = session.scalars(
            select(Candidate).where(Candidate.kind == CandidateKind.SEARCH.value)
        ).all()
        assert any(c.config == {"tp": 4} for c in untried)


def test_off_by_default_leaves_a_campaign_behaving_exactly_as_before():
    supervisor, factory = _stack(space=TWO_POINTS, confirm_top_k=0)
    with factory() as session:
        _measured(session, 1, 1, {"tp": 2}, 68000.0)
        _measured(session, 2, 2, {"tp": 4}, 43000.0)
        session.commit()

    supervisor.tick()

    assert not _candidates(factory)
    with factory() as session:
        assert session.get(Campaign, 1).status == CampaignStatus.DONE.value


def test_an_infeasible_config_is_never_confirmed():
    """Repeating a config that already breached an SLO spends a machine-hour
    to re-learn something the objective already settled."""
    supervisor, factory = _stack(space=TWO_POINTS, confirm_top_k=2, confirm_repeats=3)
    with factory() as session:
        _measured(session, 1, 1, {"tp": 2}, 99000.0, pass_rate=0.5)  # fastest, rejected
        _measured(session, 2, 2, {"tp": 4}, 43000.0)
        session.commit()

    supervisor.tick()

    configs = {tuple(sorted(c.config.items())) for c in _candidates(factory)}
    assert configs == {(("tp", 4),)}


def test_ranking_for_confirmation_uses_the_mean_not_the_best_sample():
    """A config leading on one lucky measurement is exactly what this exists
    to catch, so it must not be ranked on its luckiest run."""
    supervisor, factory = _stack(space=TWO_POINTS, confirm_top_k=1, confirm_repeats=4)
    with factory() as session:
        # tp=2: one great sample and one poor one, mean 55000.
        _measured(session, 1, 1, {"tp": 2}, 90000.0)
        _measured(session, 2, 2, {"tp": 2}, 20000.0)
        # tp=4: consistent, mean 60000 — the better bet.
        _measured(session, 3, 3, {"tp": 4}, 60000.0)
        session.commit()

    supervisor.tick()

    assert all(c.config == {"tp": 4} for c in _candidates(factory))


def test_the_campaign_finishes_once_the_repeats_are_done():
    supervisor, factory = _stack(confirm_top_k=1, confirm_repeats=2)
    with factory() as session:
        _measured(session, 1, 1, {"tp": 2}, 68000.0)
        _measured(session, 2, 2, {"tp": 2}, 68100.0)
        session.commit()

    supervisor.tick()

    assert not _candidates(factory)
    with factory() as session:
        assert session.get(Campaign, 1).status == CampaignStatus.DONE.value


# -- how it reads in the morning ---------------------------------------------


class _Run:
    def __init__(self, run_id, config, status="succeeded", kind="experiment"):
        self.id = run_id
        self.kind = kind
        self.status = status
        self.failure_class = ""
        self.error = ""
        self.machine_id = 1
        self.candidate = type("C", (), {"config": config})()


class _Result:
    def __init__(self, run_id, value, breaches=()):
        self.run_id = run_id
        self.metrics = {"tpm_card": value}
        self.objective_value = value
        self.feasible = not breaches
        self.constraints = []
        self.breaches = list(breaches)


def _campaign(**kw):
    return type("Campaign", (), {
        "name": "confirm", "objective": SLO, "model_path": "/m",
        "engine": "sglang", "image": "img", "search_space": {"grid": {"tp": [2, 4]}},
        **kw,
    })()


MACHINES = {1: type("M", (), {"name": "node-24"})()}


def test_repeated_measurements_are_one_row_with_a_spread():
    runs = [_Run(1, {"tp": 2}), _Run(2, {"tp": 2}), _Run(3, {"tp": 2})]
    results = {1: _Result(1, 68000.0), 2: _Result(2, 68200.0), 3: _Result(3, 68100.0)}
    report = render_campaign_report(_campaign(), runs, results, MACHINES)

    assert "Measurements" in report and "Spread" in report
    assert "| 3 |" in report, "three runs collapse to one row, not three rows"
    assert report.count("tp=2") < 4, "the config is not listed once per run"


def test_a_lead_narrower_than_its_own_spread_is_called_unstable():
    """Averaging disagreeing repeats into a headline reports a coin flip as a
    finding."""
    runs = [
        _Run(1, {"__baseline__": "prod"}, kind="baseline"),
        _Run(2, {"tp": 2}), _Run(3, {"tp": 2}),
    ]
    results = {
        1: _Result(1, 50000.0),
        # Mean 51000 is +2% over production, but the two samples differ by 20%.
        2: _Result(2, 46000.0), 3: _Result(3, 56000.0),
    }
    report = render_campaign_report(_campaign(), runs, results, MACHINES)

    assert "Unstable" in report
    assert "beats production" not in report


def test_a_config_that_breached_on_any_repeat_is_not_crowned():
    """The node-24 case: identical settings, clean on one run and a functional
    regression on another. Averaging that away would promote it."""
    runs = [_Run(1, {"tp": 4}), _Run(2, {"tp": 4}), _Run(3, {"tp": 2})]
    results = {
        1: _Result(1, 90000.0),
        2: _Result(2, 89000.0, breaches=["pass_rate=0.98 >= 0.99"]),
        3: _Result(3, 50000.0),
    }
    report = render_campaign_report(_campaign(), runs, results, MACHINES)

    verdict = report.split("## Results")[0]
    assert "tp=2" in verdict, "the config that held on every measurement leads"
    assert "crossed one of the objective's redlines" in report
    assert "held on 1 of 2 measurements" in report, "say how often it was fine"


def test_a_confirmed_winner_says_what_it_rests_on():
    runs = [
        _Run(1, {"__baseline__": "prod"}, kind="baseline"),
        _Run(2, {"tp": 2}), _Run(3, {"tp": 2}),
    ]
    results = {1: _Result(1, 50000.0), 2: _Result(2, 68000.0), 3: _Result(3, 68100.0)}
    report = render_campaign_report(_campaign(), runs, results, MACHINES)

    assert "confirmed over 2 measurements" in report
    assert "beats production" in report


def test_an_unconfirmed_winner_recommends_confirming_it():
    runs = [_Run(1, {"__baseline__": "prod"}, kind="baseline"), _Run(2, {"tp": 2})]
    results = {1: _Result(1, 50000.0), 2: _Result(2, 68000.0)}
    report = render_campaign_report(_campaign(), runs, results, MACHINES)

    assert "single measurement" in report
    assert "confirm_top_k" in report, "point at the knob that fixes it"
