"""The baseline as a measured reference (Stage 2).

A baseline is the production config, resolved by (served model, engine, card
type). It is measured on the same dataset as the candidates it anchors — in
place when production already serves it (the shortcut), by relaunching it when
production runs something else — and it spans BOTH stages, so the verify stage
finally has a drift-robust reference to rank against.
"""

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.control.baseline import resolve_baseline, same_config
from app.control.orchestrator.supervisor import Supervisor
from app.control.search import CandidateConfig
from app.db.base import Base
from app.db.models import (
    Baseline,
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
    RunKind,
    RunStatus,
    User,
    is_baseline_candidate,
    is_in_place_baseline,
)
from app.staging import SCREEN, VERIFY, stage_of
from tests.fakes import NullDriver

PROD_CONFIG = {"tp": "2", "chunked_prefill_size": "32768", "enable_cache_report": True}


def _prod_service(config=PROD_CONFIG):
    return {
        "container": "sglang-p8050", "endpoint_url": "http://10.0.0.1:8050",
        "served_model_name": "glm-5", "port": "8050", "cards": 2,
        "engine_args": dict(config),
    }


def _platform(*, baseline_args=None, prod=True, card_type="A100",
              staged=True, canary=True):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add(User(id=1, username="u", password_hash="x"))
        session.add(Machine(
            id=1, name="node-24", host="10.0.0.1", gpu_count=8, gpu_type=card_type,
            state=MachineState.AVAILABLE.value,
            baseline={"services": [_prod_service()]} if prod else {"services": []},
            baseline_status=BaselineStatus.CAPTURED.value if prod
            else BaselineStatus.CLEARED.value,
        ))
        session.add(Campaign(
            id=1, owner_id=1, name="c", engine="sglang", image="img",
            model_path="/m", served_model_name="glm-5",
            search_space={"grid": {"tp": [2]}},
            objective={"target_metric": "perf_guidellm_sweep.output_tpm_card_norm"},
            benchmark_slug="autotune-test-v0",
            status=CampaignStatus.ACTIVE.value, run_baseline_canary=canary,
            verify_benchmark_slug="rolling-replay-test-mf-v0" if staged else "",
            verify_top_k=1 if staged else 0,
            verify_objective={"target_metric": "replay_prod.score_card_norm"},
        ))
        if baseline_args is not None:
            session.add(Baseline(
                served_model_name="glm-5", engine="sglang", card_type=card_type,
                engine_args=baseline_args, source="manual",
            ))
        session.commit()
    supervisor = Supervisor(session_factory=factory)
    supervisor.driver = NullDriver()
    return supervisor, factory


def _screened(session, run_id, candidate_id, config, value, is_baseline=False,
              kind=CandidateKind.SEARCH.value):
    session.add(Candidate(id=candidate_id, campaign_id=1, config=config,
                          config_hash=CandidateConfig(engine_args=config).hash,
                          kind=kind, is_baseline=is_baseline,
                          status=CandidateStatus.EXHAUSTED.value))
    session.add(Run(id=run_id, campaign_id=1, candidate_id=candidate_id,
                    machine_id=1, status=RunStatus.SUCCEEDED.value))
    session.add(Result(
        run_id=run_id, source="llmbench", passed=True,
        metrics={"perf_guidellm_sweep.output_tpm_card_norm": value,
                 "functional_acceptance.pass_rate": 1.0},
        objective_value=value, feasible=True, breaches=[],
    ))


# -- the model: baseline-ness is orthogonal to stage --------------------------


def test_a_baseline_can_live_in_the_verify_stage():
    """The bug Stage 2 fixes: baseline used to BE a kind, so it was mutually
    exclusive with VERIFICATION and could never reach the verify stage."""
    verify_baseline = Candidate(
        campaign_id=1, config={"tp": 2}, config_hash="x",
        kind=CandidateKind.VERIFICATION.value, is_baseline=True,
    )
    assert is_baseline_candidate(verify_baseline)
    assert stage_of(verify_baseline) == VERIFY
    # It is launched like any candidate, so it is not an in-place baseline.
    assert not is_in_place_baseline(verify_baseline)


def test_resolve_and_compare():
    _, factory = _platform(baseline_args={"tp": "2"}, card_type="A100")
    with factory() as session:
        assert resolve_baseline(session, "glm-5", "sglang", "A100") is not None
        # A different card type is not this campaign's reference.
        assert resolve_baseline(session, "glm-5", "sglang", "L40S") is None
    # str/int are the same launch; placement already stripped upstream.
    assert same_config({"tp": "2"}, {"tp": 2})
    assert not same_config({"tp": "2"}, {"tp": "4"})


# -- the verify gap is closed -------------------------------------------------


def test_the_verify_stage_gets_a_relaunched_baseline():
    """A staged campaign with a defined baseline verifies it too — not in the
    shortlist, but as the reference the verify stage is ranked against."""
    supervisor, factory = _platform(baseline_args={"tp": "8"}, prod=False)
    with factory() as session:
        # Screening is complete: a real candidate and the screen baseline are
        # both measured, so the campaign is ready to verify.
        _screened(session, 1, 1, {"tp": 2}, 68000.0)
        _screened(session, 90, 90, {"tp": "8"}, 40000.0, is_baseline=True)
        session.commit()

    supervisor.tick()

    with factory() as session:
        verify_baselines = [
            c for c in session.scalars(select(Candidate)).all()
            if is_baseline_candidate(c) and stage_of(c) == VERIFY
        ]
        assert len(verify_baselines) == 1
        assert verify_baselines[0].config == {"tp": "8"}, "production's config, relaunched"
        assert verify_baselines[0].status == CandidateStatus.VALID.value, "it will launch"


# -- the shortcut: measure in place only when production is the baseline -------


def test_production_serving_the_baseline_is_measured_in_place():
    """The shortcut: production already runs the baseline config, so measure it
    where it stands — no relaunch, an in-place canary run."""
    supervisor, factory = _platform(baseline_args=dict(PROD_CONFIG))

    supervisor.tick()

    with factory() as session:
        runs = session.scalars(select(Run)).all()
        baseline_runs = [r for r in runs if r.kind == RunKind.BASELINE.value]
        assert len(baseline_runs) == 1, "an in-place canary against production"
        # No relaunch screen baseline was created — the shortcut stood in for it.
        screen_baselines = [
            c for c in session.scalars(select(Candidate)).all()
            if is_baseline_candidate(c) and stage_of(c) == SCREEN
            and not is_in_place_baseline(c)
        ]
        assert not screen_baselines


def test_production_running_something_else_relaunches_the_baseline():
    """Production serves a different config than the defined baseline, so the
    in-place shortcut does not apply: the baseline is relaunched as a config the
    pipeline screens itself, and clearing is not gated on a canary."""
    supervisor, factory = _platform(baseline_args={"tp": "4", "enable_dp_attention": True})

    supervisor.tick()

    with factory() as session:
        # A relaunch screen baseline was created (launchable, real engine args)…
        relaunch = [
            c for c in session.scalars(select(Candidate)).all()
            if is_baseline_candidate(c) and stage_of(c) == SCREEN
            and not is_in_place_baseline(c)
        ]
        assert len(relaunch) == 1
        assert relaunch[0].config == {"tp": "4", "enable_dp_attention": True}
        # …and it was LAUNCHED, not retired: clearing was not gated on a canary
        # (production is not the reference), so the machine freed and it ran.
        its_runs = [r for r in session.scalars(select(Run)).all()
                    if r.candidate_id == relaunch[0].id]
        assert its_runs and its_runs[0].kind == RunKind.EXPERIMENT.value
        # No in-place canary run — production is not the reference here.
        assert not [r for r in session.scalars(select(Run)).all()
                    if r.kind == RunKind.BASELINE.value]
