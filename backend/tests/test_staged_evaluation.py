"""Two stages: screen every candidate cheaply, verify the shortlist properly.

A guidellm sweep costs minutes and uses random tokens, so it cannot see prefix
cache reuse at all; a replay of real production requests costs the better part
of an hour and measures the thing we actually care about. Running the second on
every point of a grid spends a night on four candidates.

The trap this file exists to keep shut: the two benchmarks report DISJOINT
metric names. Scoring a replay result against the screening objective yields
None, which every reader downstream — the planner, the leaderboard, the report,
the campaign-finished check — reads as "this run measured nothing".
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
    PolicySession,
    PolicyTrial,
    Result,
    Run,
    RunKind,
    RunStatus,
    User,
)
from app.reporting import render_campaign_report
from app.staging import SCREEN, VERIFY, benchmark_slug, is_staged
from app.staging import objective as stage_objective
from tests.fakes import NullDriver

SCREEN_OBJECTIVE = {
    "target_metric": "perf_guidellm_sweep.output_tpm_card_norm",
    "redlines": [{"metric": "functional_acceptance.pass_rate", "op": ">=", "value": 0.99}],
}
REPLAY_OBJECTIVE = {
    "target_metric": "replay.score_card_norm",
    "redlines": [{"metric": "replay.uptime", "op": ">=", "value": 0.95}],
}
GRID = {"grid": {"tp": [2, 4]}}


def _stack(*, verify_top_k=1, verify_slug="rolling-replay-test-mf-v0", space=None, **kw):
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
                     search_space=space or {"grid": {"tp": [2]}},
                     objective=SCREEN_OBJECTIVE,
                     benchmark_slug="autotune-test-v0",
                     status=CampaignStatus.ACTIVE.value,
                     run_baseline_canary=False,
                     verify_benchmark_slug=verify_slug,
                     verify_top_k=verify_top_k,
                     verify_objective=REPLAY_OBJECTIVE,
                     **kw)
        )
        session.commit()
    supervisor = Supervisor(session_factory=factory)
    supervisor.driver = NullDriver()
    return supervisor, factory


def _screened(session, run_id, candidate_id, config, value, pass_rate=1.0,
              status=RunStatus.SUCCEEDED.value):
    """A finished screening run, as _record_result leaves it."""
    session.add(Candidate(id=candidate_id, campaign_id=1, config=config,
                          config_hash=CandidateConfig(engine_args=config).hash,
                          status=CandidateStatus.EXHAUSTED.value))
    session.add(Run(id=run_id, campaign_id=1, candidate_id=candidate_id,
                    machine_id=1, status=status))
    session.add(Result(
        run_id=run_id, source="llmbench", passed=True,
        metrics={"perf_guidellm_sweep.output_tpm_card_norm": value,
                 "functional_acceptance.pass_rate": pass_rate},
        objective_value=value,
        feasible=pass_rate >= 0.99,
        breaches=[] if pass_rate >= 0.99 else ["pass_rate below 0.99"],
    ))


def _verify_candidates(factory):
    with factory() as session:
        return session.scalars(
            select(Candidate)
            .where(Candidate.kind == CandidateKind.VERIFICATION.value)
            .order_by(Candidate.id)
        ).all()


# -- when the expensive stage fires -------------------------------------------


def test_the_best_screened_config_is_sent_to_the_expensive_benchmark():
    supervisor, factory = _stack(space=GRID, verify_top_k=1)
    with factory() as session:
        _screened(session, 1, 1, {"tp": 2}, 68000.0)
        _screened(session, 2, 2, {"tp": 4}, 43000.0)
        session.commit()

    supervisor.tick()

    queued = _verify_candidates(factory)
    assert len(queued) == 1
    assert queued[0].config == {"tp": 2}, "the leader, not the field"
    assert queued[0].status == CandidateStatus.VALID.value
    with factory() as session:
        assert session.get(Campaign, 1).status == CampaignStatus.ACTIVE.value


def test_the_search_is_exhausted_before_anything_is_verified():
    """A replay costs an hour. Spending one on an early leader while the grid
    still has unexplored points is how a night ends with the expensive stage
    run on a config the search would have beaten."""
    supervisor, factory = _stack(space=GRID, verify_top_k=1)
    with factory() as session:
        _screened(session, 1, 1, {"tp": 2}, 68000.0)  # tp=4 never tried
        session.commit()

    supervisor.tick()

    assert not _verify_candidates(factory)


def test_confirmation_comes_first_when_both_are_on():
    """Cheap repeats settle whether a lead is real; only then is it worth an
    hour of replay. The other order buys the expensive measurement for whoever
    got the luckiest single sample."""
    supervisor, factory = _stack(space=GRID, verify_top_k=1,
                                 confirm_top_k=1, confirm_repeats=3)
    with factory() as session:
        _screened(session, 1, 1, {"tp": 2}, 68000.0)
        _screened(session, 2, 2, {"tp": 4}, 43000.0)
        session.commit()

    supervisor.tick()

    assert not _verify_candidates(factory), "repeats first"
    with factory() as session:
        repeats = session.scalars(
            select(Candidate).where(
                Candidate.kind == CandidateKind.CONFIRMATION.value
            )
        ).all()
        assert len(repeats) == 2


def test_a_verification_candidate_keeps_the_hash_of_the_point_it_verifies():
    """That hash is how a planner knows a config was tried. A re-measurement
    that looked like a new point would make the search think it had explored
    more of the space than it had."""
    supervisor, factory = _stack(verify_top_k=1)
    with factory() as session:
        _screened(session, 1, 1, {"tp": 2}, 68000.0)
        session.commit()

    supervisor.tick()

    queued = _verify_candidates(factory)[0]
    assert queued.config_hash == CandidateConfig(engine_args={"tp": 2}).hash
    assert queued.repeat_of == 1


def test_an_infeasible_config_is_never_verified():
    """It already crossed a redline on the cheap test. Spending an hour to
    confirm that is an hour the next candidate does not get."""
    supervisor, factory = _stack(space=GRID, verify_top_k=2)
    with factory() as session:
        _screened(session, 1, 1, {"tp": 2}, 99000.0, pass_rate=0.5)  # fastest, rejected
        _screened(session, 2, 2, {"tp": 4}, 43000.0)
        session.commit()

    supervisor.tick()

    assert [c.config for c in _verify_candidates(factory)] == [{"tp": 4}]


# -- when it does not ---------------------------------------------------------


def test_a_campaign_without_a_second_benchmark_is_unchanged():
    supervisor, factory = _stack(space=GRID, verify_slug="", verify_top_k=0)
    with factory() as session:
        _screened(session, 1, 1, {"tp": 2}, 68000.0)
        _screened(session, 2, 2, {"tp": 4}, 43000.0)
        session.commit()

    supervisor.tick()

    assert not _verify_candidates(factory)
    with factory() as session:
        assert session.get(Campaign, 1).status == CampaignStatus.DONE.value


def test_half_a_staged_campaign_stages_nothing():
    """A slug with no top-k, or a top-k with no slug, is configuration that
    silently never fires. The API refuses both; the supervisor must not act on
    one either, or an edit straight into the database re-runs the cheap
    benchmark and the report calls it verification."""
    for slug, top_k in (("some-slug", 0), ("", 2)):
        supervisor, factory = _stack(verify_slug=slug, verify_top_k=top_k)
        with factory() as session:
            _screened(session, 1, 1, {"tp": 2}, 68000.0)
            session.commit()

        supervisor.tick()

        assert not _verify_candidates(factory), f"slug={slug!r} top_k={top_k}"


def test_verification_happens_once_per_config_and_the_campaign_then_finishes():
    """A repeat costs the better part of an hour. Queuing another because the
    first one failed is how a campaign spends a second night on one config."""
    supervisor, factory = _stack(verify_top_k=1)
    with factory() as session:
        _screened(session, 1, 1, {"tp": 2}, 68000.0)
        session.commit()

    supervisor.tick()
    assert len(_verify_candidates(factory)) == 1

    # That verification runs and fails outright.
    with factory() as session:
        queued = _verify_candidates(factory)[0]
        candidate = session.get(Candidate, queued.id)
        candidate.status = CandidateStatus.EXHAUSTED.value
        session.add(Run(id=50, campaign_id=1, candidate_id=candidate.id,
                        machine_id=1, status=RunStatus.FAILED.value,
                        failure_class="bench_failed"))
        session.commit()

    supervisor.tick()

    assert len(_verify_candidates(factory)) == 1, "no second attempt"
    with factory() as session:
        assert session.get(Campaign, 1).status == CampaignStatus.DONE.value


# -- which benchmark, which objective -----------------------------------------


def test_each_stage_submits_to_its_own_benchmark():
    supervisor, factory = _stack(verify_top_k=1)
    with factory() as session:
        campaign = session.get(Campaign, 1)
        assert benchmark_slug(campaign, SCREEN) == "autotune-test-v0"
        assert benchmark_slug(campaign, VERIFY) == "rolling-replay-test-mf-v0"
        assert is_staged(campaign)


def test_a_verification_run_is_judged_by_the_replay_objective():
    """The bug this prevents is silent: the run succeeds, LLMBench returns a
    full metric set, and scoring it against `perf_guidellm_sweep.*` produces
    None — so the campaign finishes reporting that its expensive stage
    measured nothing."""
    supervisor, factory = _stack(verify_top_k=1)
    with factory() as session:
        _screened(session, 1, 1, {"tp": 2}, 68000.0)
        session.commit()
    supervisor.tick()

    replay_metrics = {
        "replay.score_card_norm": 4565217.07,
        "replay.uptime": 1.0,
    }
    with factory() as session:
        campaign = session.get(Campaign, 1)
        candidate = _verify_candidates(factory)[0]
        run = Run(id=60, campaign_id=1, candidate_id=candidate.id, machine_id=1,
                  status=RunStatus.BENCHING.value)
        session.add(run)
        session.flush()

        supervisor._record_result(
            session, run, "llmbench",
            type("Outcome", (), {"status": "passed", "metrics": replay_metrics,
                                 "raw": {}})(),
        )
        session.commit()

        stored = session.scalars(select(Result).where(Result.run_id == 60)).one()
        assert stored.objective_value == 4565217.07
        assert stored.feasible
        assert stage_objective(campaign, VERIFY) == REPLAY_OBJECTIVE


def test_a_platform_benchmark_becomes_a_policy_trial():
    """The contract promise: a platform-measured benchmark auto-becomes a trial
    with source 'platform'. The reference policy never self-reports these, so if
    the platform does not record them at result time the trial ledger — the UI's
    live stream and the cross-night warm-start history — stays empty."""
    supervisor, factory = _stack()
    with factory() as session:
        session.add(PolicySession(id=1, campaign_id=1, policy_id=1, machine_id=1,
                                  status="searching"))
        cand = Candidate(id=1, campaign_id=1, config={"tp": 2},
                         config_hash=CandidateConfig(engine_args={"tp": 2}).hash)
        session.add(cand)
        session.flush()
        run = Run(id=90, campaign_id=1, candidate_id=cand.id, machine_id=1,
                  policy_session_id=1, kind=RunKind.EXTERNAL.value,
                  status=RunStatus.BENCHING.value)
        session.add(run)
        session.flush()
        outcome = type("Outcome", (), {"status": "passed", "raw": {},
                                       "metrics": {"perf_guidellm_sweep.output_tps": 1234.0}})()
        supervisor._record_result(session, run, "llmbench", outcome)
        supervisor._record_result(session, run, "llmbench", outcome)  # redelivery
        session.commit()

        trials = session.scalars(select(PolicyTrial).where(PolicyTrial.session_id == 1)).all()
        assert len(trials) == 1  # idempotent on the run
        t = trials[0]
        assert t.source == "platform"
        assert t.run_id == 90
        assert t.config == {"tp": 2}
        assert t.reported_metrics == {"perf_guidellm_sweep.output_tps": 1234.0}


def test_a_non_policy_benchmark_records_no_trial():
    """A classic campaign's run is not a policy session; recording its result
    must not manufacture a trial."""
    supervisor, factory = _stack()
    with factory() as session:
        cand = Candidate(id=1, campaign_id=1, config={"tp": 2},
                         config_hash=CandidateConfig(engine_args={"tp": 2}).hash)
        session.add(cand)
        session.flush()
        run = Run(id=91, campaign_id=1, candidate_id=cand.id, machine_id=1,
                  status=RunStatus.BENCHING.value)
        session.add(run)
        session.flush()
        outcome = type("Outcome", (), {"status": "passed", "raw": {},
                                       "metrics": {"perf_guidellm_sweep.output_tps": 1.0}})()
        supervisor._record_result(session, run, "llmbench", outcome)
        session.commit()
        assert session.scalars(select(PolicyTrial)).all() == []


def test_a_verification_result_never_reaches_the_screening_ranking():
    """Its metrics are named differently, so read on the screening scale it is
    a config that produced nothing — i.e. a reason to avoid the best point the
    campaign has. The two stages are never averaged or compared."""
    supervisor, factory = _stack(verify_top_k=1)
    with factory() as session:
        _screened(session, 1, 1, {"tp": 2}, 68000.0)
        session.commit()
    supervisor.tick()

    with factory() as session:
        campaign = session.get(Campaign, 1)
        candidate = _verify_candidates(factory)[0]
        session.add(Run(id=70, campaign_id=1, candidate_id=candidate.id,
                        machine_id=1, status=RunStatus.SUCCEEDED.value))
        session.add(Result(run_id=70, source="llmbench", passed=True,
                           metrics={"replay.score_card_norm": 9.0},
                           objective_value=9.0, feasible=True, breaches=[]))
        session.commit()

        ranked = supervisor._ranked_screen_configs(session, campaign)
        values = [value for _, _, value in ranked]
        assert 9.0 not in values, "the replay scale must not reach the screening ranking"
        assert 68000.0 in values


def test_ranking_for_verification_ignores_verification_results():
    """Otherwise the second config to be verified is ranked against the first
    one's replay score, on a scale hundreds of times larger."""
    supervisor, factory = _stack(space=GRID, verify_top_k=2)
    with factory() as session:
        _screened(session, 1, 1, {"tp": 2}, 68000.0)
        _screened(session, 2, 2, {"tp": 4}, 43000.0)
        session.commit()
    supervisor.tick()

    with factory() as session:
        campaign = session.get(Campaign, 1)
        # tp=4's replay lands with a huge number on the replay scale.
        candidate = [c for c in _verify_candidates(factory) if c.config == {"tp": 4}][0]
        session.add(Run(id=80, campaign_id=1, candidate_id=candidate.id,
                        machine_id=1, status=RunStatus.SUCCEEDED.value))
        session.add(Result(run_id=80, source="llmbench", passed=True,
                           metrics={"replay.score_card_norm": 4_565_217.0},
                           objective_value=4_565_217.0, feasible=True, breaches=[]))
        session.commit()

        ranked = supervisor._ranked_screen_configs(session, campaign)
        leader = ranked[0]
        assert leader[2] == 68000.0, "tp=2 still leads the screening ranking"


# -- fitting the night --------------------------------------------------------


def test_a_verification_run_reserves_its_own_share_of_the_window():
    """Starting an hour-long replay ninety minutes before hand-back means
    killing it at the cutoff, having spent the slot and learned nothing."""
    from datetime import UTC, datetime, timedelta

    supervisor, factory = _stack(verify_top_k=1, max_run_minutes=30,
                                 verify_max_run_minutes=180)
    with factory() as session:
        campaign = session.get(Campaign, 1)
        # An hour of window left: room for a screening run, not for a replay.
        campaign.window_end = datetime.now(UTC) + timedelta(minutes=60)
        _screened(session, 1, 1, {"tp": 2}, 68000.0)
        session.commit()

    supervisor.tick()  # queues the verification candidate
    assert len(_verify_candidates(factory)) == 1
    supervisor.tick()  # and declines to start it

    with factory() as session:
        started = session.scalars(
            select(Run).where(Run.candidate_id == _verify_candidates(factory)[0].id)
        ).all()
        assert not started, "no replay started with an hour of night left"
        assert session.get(Candidate, _verify_candidates(factory)[0].id).status == (
            CandidateStatus.VALID.value
        ), "still queued for the next window, not discarded"


# -- how it reads in the morning ----------------------------------------------


class _Run:
    def __init__(self, run_id, config, stage=SCREEN, status="succeeded",
                 kind="experiment"):
        self.id = run_id
        self.kind = kind
        self.stage = stage
        self.status = status
        self.failure_class = ""
        self.error = ""
        self.machine_id = 1
        self.candidate = type("C", (), {"config": config})()


class _Result:
    def __init__(self, run_id, value, breaches=()):
        self.run_id = run_id
        self.metrics = {}
        self.objective_value = value
        self.feasible = not breaches
        self.constraints = []
        self.breaches = list(breaches)


def _campaign(**kw):
    return type("Campaign", (), {
        "name": "staged", "objective": SCREEN_OBJECTIVE, "model_path": "/m",
        "engine": "sglang", "image": "img", "search_space": GRID,
        "verify_benchmark_slug": "rolling-replay-test-mf-v0",
        "verify_objective": REPLAY_OBJECTIVE,
        **kw,
    })()


MACHINES = {1: type("M", (), {"name": "node-24"})()}


def test_the_report_keeps_the_two_scales_in_separate_tables():
    runs = [
        _Run(1, {"tp": 2}), _Run(2, {"tp": 4}),
        _Run(3, {"tp": 2}, stage=VERIFY),
    ]
    results = {1: _Result(1, 68000.0), 2: _Result(2, 43000.0),
               3: _Result(3, 4_565_217.0)}
    report = render_campaign_report(_campaign(), runs, results, MACHINES)

    assert "## Verified on `rolling-replay-test-mf-v0`" in report
    assert "## Screening results" in report
    verified_section = report.split("## Screening")[0]
    assert "4,565,217.0" in verified_section
    assert "68,000.0" not in verified_section, "screening numbers stay below"


def test_the_report_does_not_quote_a_percentage_against_a_baseline_it_never_replayed():
    """The canary measures production with the SCREENING benchmark. A "+18% vs
    production" on the replay table would be two different workloads compared
    to each other and presented as a finding."""
    runs = [
        _Run(1, {"__baseline__": "prod"}, kind="baseline"),
        _Run(2, {"tp": 2}),
        _Run(3, {"tp": 2}, stage=VERIFY),
    ]
    results = {1: _Result(1, 50000.0), 2: _Result(2, 68000.0),
               3: _Result(3, 4_565_217.0)}
    report = render_campaign_report(_campaign(), runs, results, MACHINES)

    verified_section = report.split("## Screening")[0]
    assert "%" not in verified_section
    assert "not this one" in verified_section, "say why there is no comparison"


def test_a_verified_candidate_that_crossed_a_redline_is_not_crowned():
    """The whole point of the expensive stage: real traffic can break a config
    the synthetic sweep liked."""
    runs = [_Run(1, {"tp": 2}), _Run(2, {"tp": 2}, stage=VERIFY)]
    results = {1: _Result(1, 68000.0),
               2: _Result(2, 4_565_217.0, breaches=["uptime=0.91 >= 0.95"])}
    report = render_campaign_report(_campaign(), runs, results, MACHINES)

    verified_section = report.split("## Screening")[0]
    assert "crossed a redline on real traffic" in verified_section
    assert "uptime=0.91" in verified_section


def test_an_unverified_campaign_is_told_the_platform_can_replay_for_it():
    runs = [
        _Run(1, {"__baseline__": "prod"}, kind="baseline"),
        _Run(2, {"tp": 2}), _Run(3, {"tp": 2}),
    ]
    results = {1: _Result(1, 50000.0), 2: _Result(2, 68000.0), 3: _Result(3, 68100.0)}
    report = render_campaign_report(
        _campaign(verify_benchmark_slug=""), runs, results, MACHINES
    )

    assert "verify_benchmark_slug" in report


def test_a_verified_campaign_is_not_told_to_go_and_validate_it():
    runs = [
        _Run(1, {"__baseline__": "prod"}, kind="baseline"),
        _Run(2, {"tp": 2}), _Run(3, {"tp": 2}),
        _Run(4, {"tp": 2}, stage=VERIFY),
    ]
    results = {1: _Result(1, 50000.0), 2: _Result(2, 68000.0),
               3: _Result(3, 68100.0), 4: _Result(4, 4_565_217.0)}
    report = render_campaign_report(_campaign(), runs, results, MACHINES)

    assert "Validate the winner against current traffic" not in report
