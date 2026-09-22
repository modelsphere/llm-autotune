"""Executing one trial, with no candidate-generation strategy involved.

The brief's `TrialExecutor.execute(config) -> TrialResult` is a conceptual
layer, and this is its conceptual test: hand-build a configuration, run it end
to end, get a structured result back — without grid search, random search, or
any planner at all.

The platform does not implement that as a blocking call, because a trial here
takes ~90 minutes and must survive a worker restart (see
docs/findings_claude/searcher-architecture-review.md §D.1). It implements the
same responsibilities as a resumable state machine over a database row. The
`execute()` shape is recoverable on top in a few lines — `_execute` below is
exactly that, and it is what makes this test read like the brief's.

What the executor owns, and where each piece lives:

    build the engine command   EngineAdapter.build_command
    start the server           driver.launch          PENDING -> LAUNCHING
    wait for readiness         driver.state + _ready_timed_out
    run the benchmark          bench.start / bench.poll  -> BENCHING
    parse benchmark output     LLMBenchEvaluator.poll + _flatten_metrics
    collect logs and metadata  _capture_failure, _snapshot_environment
    stop child processes       _finish -> driver.teardown
    clean up                   teardown removes the container and its GPUs
    return a structured result Result row, read back as a RunRecord

Warm-up is the one step with no local equivalent: LLMBench owns it, and
excludes its own warm-up requests from the metrics it reports.
"""

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.control.orchestrator.supervisor import Supervisor
from app.control.search import CandidateConfig
from app.db.base import Base
from app.db.models import (
    TERMINAL_RUN_STATES,
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
from tests.fakes import CompletingDriver, StubEvaluator

METRICS = {
    "perf_guidellm_sweep.output_tpm_card_norm": 69288.3,
    "perf_guidellm_sweep.c1.request_output_tps": 129.82,
    "functional_acceptance.pass_rate": 1.0,
}
OBJECTIVE = {
    "target_metric": "perf_guidellm_sweep.output_tpm_card_norm",
    "redlines": [
        {"metric": "functional_acceptance.pass_rate", "op": ">=", "value": 0.99},
        {"metric": "perf_guidellm_sweep.c1.request_output_tps", "op": ">=", "value": 100.0},
    ],
}


def _platform(metrics=None, environment=None):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add(User(id=1, username="u", password_hash="x"))
        session.add(
            Machine(id=1, name="node-24", host="10.0.0.1", gpu_count=8, gpu_type="A100",
                    state=MachineState.AVAILABLE.value,
                    baseline_status=BaselineStatus.CLEARED.value)
        )
        session.add(
            Campaign(id=1, owner_id=1, name="manual", engine="sglang", image="img",
                     model_path="/m", served_model_name="glm-5",
                     # No space at all: nothing can be proposed even in principle.
                     search_space={}, objective=OBJECTIVE,
                     status=CampaignStatus.ACTIVE.value,
                     run_baseline_canary=False)
        )
        session.commit()

    bench = StubEvaluator(metrics if metrics is not None else METRICS)
    supervisor = Supervisor(
        session_factory=factory,
        health_evaluator=StubEvaluator({"probe_output_chars": 2}),
        bench_evaluator=bench,
    )
    supervisor.driver = CompletingDriver(environment=environment)
    return supervisor, factory, bench


def _execute(supervisor, factory, config: dict, max_ticks: int = 20):
    """The brief's `execute(config) -> result`, over the tick loop.

    Insert the configuration as a candidate, advance until the run reaches a
    terminal state, and hand back what was recorded. No planner is consulted:
    nothing proposes candidates here, and the search space is empty.
    """
    with factory() as session:
        session.add(
            Candidate(
                campaign_id=1,
                config=config,
                config_hash=CandidateConfig(engine_args=config).hash,
                status=CandidateStatus.VALID.value,
            )
        )
        session.commit()

    terminal = {s.value for s in TERMINAL_RUN_STATES}
    for _ in range(max_ticks):
        supervisor.tick()
        with factory() as session:
            run = session.scalars(select(Run).order_by(Run.id.desc())).first()
            if run is not None and run.status in terminal:
                result = session.scalars(
                    select(Result).where(
                        Result.run_id == run.id, Result.source == "llmbench"
                    )
                ).first()
                return run, result
    raise AssertionError("run never reached a terminal state")


CONFIG = {"tp": 2, "attention_backend": "flashinfer", "chunked_prefill_size": 32768}


def test_a_hand_built_configuration_runs_end_to_end():
    supervisor, factory, _ = _platform()
    run, result = _execute(supervisor, factory, CONFIG)

    assert run.status == RunStatus.SUCCEEDED.value
    assert result is not None

    # A structured result, not a metrics blob: scored and judged feasible
    # against the campaign's objective when it landed.
    assert result.objective_value == 69288.3
    assert result.feasible
    assert not result.breaches
    assert result.constraints == [0.99 - 1.0, 100.0 - 129.82]

    with factory() as session:
        assert not session.scalars(
            select(Candidate).where(Candidate.campaign_id == 1)
        ).all()[1:], "the empty space contributed nothing"


def test_the_lifecycle_cleans_up_after_itself():
    """Teardown removes the container, which is what releases its GPUs, and
    the machine goes back to available for the next trial."""
    supervisor, factory, _ = _platform()
    run, _ = _execute(supervisor, factory, CONFIG)

    assert supervisor.driver.torn_down == [f"autotune-run-{run.id}"]
    with factory() as session:
        assert session.get(Machine, 1).state == MachineState.AVAILABLE.value
        assert not session.scalars(
            select(Run).where(Run.status.not_in([s.value for s in TERMINAL_RUN_STATES]))
        ).all()


def test_the_command_and_environment_are_recorded_for_reproduction():
    supervisor, factory, _ = _platform(
        environment={"image_digest": "sha256:abc", "engine_version": "0.5.10.post1"}
    )
    run, _ = _execute(supervisor, factory, CONFIG)

    assert run.launch_command, "the exact command that ran"
    assert run.env_snapshot["engine_version"] == "0.5.10.post1"
    assert run.env_snapshot["image_digest"] == "sha256:abc"
    # GPU model comes off the machine row, so relabelling it later cannot
    # rewrite what this run actually used.
    assert run.env_snapshot["machine_gpu_type"] == "A100"
    assert run.env_snapshot["gpu_indices"] == run.gpu_indices


def test_an_infeasible_result_still_completes_and_is_recorded():
    """Breaching a redline is a verdict on the configuration, not an error:
    the run succeeds, and the result carries the reason it is unusable."""
    supervisor, factory, _ = _platform(
        metrics={**METRICS, "functional_acceptance.pass_rate": 0.98}
    )
    run, result = _execute(supervisor, factory, CONFIG)

    assert run.status == RunStatus.SUCCEEDED.value, "it ran fine; it just is not eligible"
    assert not result.feasible
    assert result.objective_value == 69288.3, "still scored, so it can be compared"
    assert "functional_acceptance.pass_rate" in result.breaches[0]
    assert result.constraints[0] > 0, "crossed reads positive"
