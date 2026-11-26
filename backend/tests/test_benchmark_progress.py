"""Results while a benchmark runs (contract 1.8).

LLMBench stores a module's metrics the moment it finishes and lists every
module run of a submission from the start. The platform keeps what is done
on the run at each poll, so a policy can read the first workload of a long
benchmark while the next one still runs, and decide whether to wait.
"""
# ruff: noqa: F811  (the `night` fixture is imported, then named as a parameter)

import httpx

from app.db.models import Run
from app.evaluation.base import EvalOutcome, EvalStatus, Evaluator
from app.evaluation.llmbench import LLMBenchEvaluator, progress_of
from tests.test_policy_sessions import (  # noqa: F401  (fixture)
    _boot,
    _serving_launch,
    night,
)

SWEEP = "perf_guidellm_sweep"


def _submission(status: str, *runs: tuple[str, str, dict | None]) -> dict:
    return {
        "id": 9, "status": status, "passed": None, "score_total": None,
        "runs": [
            {"id": 90 + i, "module_name": name, "status": state, "score": None,
             "metrics_json": metrics}
            for i, (name, state, metrics) in enumerate(runs)
        ],
    }


HALFWAY = _submission(
    "running",
    (SWEEP, "done", {"output_tps": 900.0, "c16": {"ttft_p99_ms": 410.0}}),
    (SWEEP, "running", None),
    ("opencompass", "pending", None),
)


def test_the_modules_that_are_done_are_read_while_the_rest_run():
    progress = progress_of(HALFWAY)

    assert progress["modules"] == [
        {"key": SWEEP, "module": SWEEP, "status": "done"},
        {"key": f"{SWEEP}#2", "module": SWEEP, "status": "running"},
        {"key": "opencompass", "module": "opencompass", "status": "pending"},
    ]
    assert progress["metrics"] == {
        f"{SWEEP}.output_tps": 900.0, f"{SWEEP}.c16.ttft_p99_ms": 410.0,
    }


def test_a_submission_with_no_runs_yet_has_no_progress():
    assert progress_of({"status": "queued", "runs": []}) == {}


class _Client:
    def __init__(self, answer):
        self.answer = answer

    def get_submission(self, submission_id):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_llmbench_answers_a_running_poll_with_its_progress():
    outcome = LLMBenchEvaluator(client=_Client(HALFWAY)).poll("9")
    assert outcome.status == EvalStatus.RUNNING
    assert outcome.progress["metrics"][f"{SWEEP}.output_tps"] == 900.0

    lost = LLMBenchEvaluator(client=_Client(httpx.ConnectError("down"))).poll("9")
    assert lost.status == EvalStatus.RUNNING and lost.progress == {}


class ScriptedEvaluator(Evaluator):
    """Answers each poll with the next outcome it was given, then the last."""

    name = "scripted"

    def __init__(self, *outcomes: EvalOutcome):
        self.outcomes = list(outcomes)

    def start(self, endpoint_url, served_model_name, context) -> str:
        return "scripted:1"

    def poll(self, external_ref) -> EvalOutcome:
        return self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]


DONE = _submission(
    "done",
    (SWEEP, "done", {"output_tps": 900.0}),
    (SWEEP, "done", {"output_tps": 300.0}),
)


async def test_a_policy_reads_the_first_workload_before_the_benchmark_ends(night):
    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)
    running = EvalOutcome(status=EvalStatus.RUNNING, progress=progress_of(HALFWAY))
    lost = EvalOutcome(status=EvalStatus.RUNNING, error="poll failed")
    finished = EvalOutcome(
        status=EvalStatus.PASSED, raw=DONE,
        metrics={"score_total": 1.0, f"{SWEEP}.output_tps": 900.0,
                 f"{SWEEP}#2.output_tps": 300.0},
    )
    supervisor.bench = ScriptedEvaluator(running, lost, finished)
    launch = await _serving_launch(supervisor, policy)
    bench = (await policy.post(f"/launches/{launch}/benchmarks", json={"suite": "screen"})).json()

    supervisor.tick()  # health check -> benching
    supervisor.tick()  # first poll: halfway
    halfway = (await policy.get(f"/benchmarks/{bench['id']}")).json()
    assert halfway["status"] == "benching"
    assert halfway["metrics"] == progress_of(HALFWAY)["metrics"]
    assert [m["status"] for m in halfway["modules"]] == ["done", "running", "pending"]
    assert halfway["summary"] is None, "nothing is judged before the end"

    supervisor.tick()  # a poll that could not reach the platform
    assert (await policy.get(f"/benchmarks/{bench['id']}")).json()["metrics"] == (
        halfway["metrics"]), "a lost poll keeps what was read"

    supervisor.tick()  # done
    end = (await policy.get(f"/benchmarks/{bench['id']}")).json()
    assert end["status"] == "succeeded"
    assert end["metrics"][f"{SWEEP}#2.output_tps"] == 300.0
    assert end["summary"]["objective_value"] == 1.0
    assert [m["key"] for m in end["modules"]] == [SWEEP, f"{SWEEP}#2"]
    assert all(m["status"] == "done" for m in end["modules"])


async def test_progress_is_written_only_when_it_changes(night):
    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)
    running = EvalOutcome(status=EvalStatus.RUNNING, progress=progress_of(HALFWAY))
    supervisor.bench = ScriptedEvaluator(running)
    launch = await _serving_launch(supervisor, policy)
    bench = (await policy.post(f"/launches/{launch}/benchmarks", json={"suite": "screen"})).json()
    supervisor.tick()
    supervisor.tick()

    async with http.db() as session:
        run = await session.get(Run, bench["id"])
        assert run.progress == progress_of(HALFWAY)
        written = run.progress

    supervisor.tick()
    async with http.db() as session:
        assert (await session.get(Run, bench["id"])).progress == written
