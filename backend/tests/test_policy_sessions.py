"""Policy-as-code, end to end and at its edges.

The centrepiece drives a whole night through the real HTTP contract: the
supervisor launches a (fake) policy container, a FakePolicy speaks the API the
way the SDK would — heartbeat, plan, delegated launch, bench, contenders,
finalize, serve — and the platform delivers the verdict. The same wiring the
mock stack proves for classic campaigns, one level up.

Everything runs on one shared-cache sqlite (the lease-API trick): the router
writes through the async session, the supervisor reads through the sync one.
"""

from datetime import UTC, datetime, timedelta
from itertools import count

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import create_engine, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.control.launch.base import WorkloadState
from app.control.orchestrator import timing
from app.control.orchestrator.supervisor import Supervisor
from app.control.search.space import deviations
from app.core import apikeys
from app.core.auth import create_token
from app.db.base import Base, get_async_session
from app.db.models import (
    ApiKey,
    Campaign,
    CampaignStatus,
    Candidate,
    ContenderStatus,
    Machine,
    MachineState,
    Policy,
    PolicyContender,
    PolicySession,
    PolicySessionStatus,
    Run,
    RunKind,
    RunStatus,
    User,
)
from app.main import app
from tests.fakes import CompletingDriver, EndlessEvaluator, StubEvaluator

_counter = count()

SPACE = {
    "base": {"enable_cache_report": True},
    "grid": {"tp_size": [1, 2, 4], "chunked_prefill_size": [2048, 4096]},
    "range": {"max_running_requests": {"min": 64, "max": 256, "step": 64}},
}


# -- deviations(): the shared definition of "outside the space" ----------------


def test_deviations_classifies_each_kind():
    config = {
        "tp_size": 8,                 # not on the grid
        "chunked_prefill_size": 4096, # declared — clean
        "max_running_requests": 512,  # above the range
        "enable_torch_compile": True, # never mentioned
        "enable_cache_report": False, # overrides base
    }
    found = {d["param"]: d["kind"] for d in deviations(config, SPACE)}
    assert found == {
        "tp_size": "off_grid",
        "max_running_requests": "out_of_range",
        "enable_torch_compile": "unknown",
        "enable_cache_report": "base_override",
    }


def test_deviations_flags_a_conditionally_inactive_param():
    space = {
        "grid": {"speculative_algorithm": ["EAGLE"]},
        "conditions": {"speculative_num_steps": {"speculative_algorithm": ["EAGLE"]}},
    }
    dev = deviations({"speculative_num_steps": 3}, space)
    assert [d["kind"] for d in dev] == ["conditional_violation"]
    assert not deviations({"speculative_algorithm": "EAGLE", "speculative_num_steps": 3}, space)


def test_an_in_space_config_has_no_deviations():
    assert deviations({"tp_size": 2, "max_running_requests": 128}, SPACE) == []


# -- the platform + a fake policy over real HTTP -------------------------------


class FakePolicy:
    """A policy container's client side, minus the container.

    Reads its credentials from the WorkloadSpec the fake driver recorded —
    exactly where a real container would read its env."""

    def __init__(self, http: httpx.AsyncClient, spec):
        self.http = http
        self.token = spec.env["AUTOTUNE_API_KEY"]
        self.session_id = int(spec.env["AUTOTUNE_SESSION_ID"])

    def _headers(self, **extra):
        return {"X-API-Key": self.token, **extra}

    async def get(self, path):
        return await self.http.get(f"/api/policy/v1{path}", headers=self._headers())

    async def post(self, path, json=None, **headers):
        return await self.http.post(
            f"/api/policy/v1{path}", json=json, headers=self._headers(**headers)
        )

    async def put(self, path, json):
        return await self.http.put(f"/api/policy/v1{path}", json=json, headers=self._headers())

    async def heartbeat(self, **body):
        response = await self.post("/session/heartbeat", json={"phase": "searching", **body})
        assert response.status_code == 200, response.text
        return response.json()


@pytest_asyncio.fixture
async def night(monkeypatch):
    """One ready-to-run policy night: campaign + machine + supervisor + HTTP."""
    name = f"policy_api_{next(_counter)}"
    db = f"sqlite+aiosqlite:///file:{name}?mode=memory&cache=shared&uri=true"
    sync_db = f"sqlite:///file:{name}?mode=memory&cache=shared&uri=true"

    engine = create_async_engine(db)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    sync_engine = create_engine(sync_db)
    sync_factory = sessionmaker(sync_engine, expire_on_commit=False)

    async def _session():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_async_session] = _session

    now = datetime.now(UTC)
    async with factory() as session:
        session.add(User(id=1, username="u", password_hash="x"))
        session.add(
            Policy(id=1, owner_id=1, name="tpe-test", image="policy-img:1", ports=2)
        )
        session.add(
            Machine(
                id=1, name="gpu-1", host="10.0.0.9", gpu_count=8, gpu_type="H100",
                state=MachineState.AVAILABLE.value,
            )
        )
        session.add(
            Campaign(
                id=1, owner_id=1, name="policy night", engine="sglang",
                image="sglang:test", model_path="/models/m", served_model_name="m",
                search_space=SPACE, objective={"target_metric": "score_total"},
                status=CampaignStatus.ACTIVE.value, machine_names=["gpu-1"],
                run_baseline_canary=False,
                window_start=now - timedelta(minutes=5),
                window_end=now + timedelta(hours=4),
                policy_id=1,
                policy_settings={"max_contenders": 1, "approx_minutes_each": 5},
            )
        )
        await session.commit()

    driver = CompletingDriver()
    supervisor = Supervisor(
        session_factory=sync_factory,
        health_evaluator=StubEvaluator({"probe_output_chars": 2}),
        bench_evaluator=StubEvaluator({"score_total": 1.0}),
    )
    supervisor.driver = driver
    monkeypatch.setattr(supervisor.settings, "public_api_url", "http://platform.test")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
        http.db = factory
        yield supervisor, driver, http

    app.dependency_overrides.clear()
    await engine.dispose()
    sync_engine.dispose()


async def _session_row(http) -> PolicySession:
    async with http.db() as session:
        return (await session.execute(select(PolicySession))).scalars().one()


async def _boot(supervisor, driver, http) -> FakePolicy:
    """tick → the machine queue reserves a machine; tick → container launched;
    first heartbeat; tick → SEARCHING."""
    supervisor.tick()
    assert not driver.workloads, "reserved in the queue, launched next tick"
    supervisor.tick()
    assert driver.workloads, "the policy container was not launched"
    policy = FakePolicy(http, driver.workloads[0])
    beat = await policy.heartbeat(
        sdk_version="0.0-test", contract_version="1.0", capabilities=["serve"]
    )
    assert beat["command"] == "run"
    supervisor.tick()
    assert (await _session_row(http)).status == PolicySessionStatus.SEARCHING.value
    return policy


async def _serving_launch(supervisor, policy) -> int:
    launched = await policy.post(
        "/launches", json={"engine_args": {"tp_size": 2}, "gpu_indices": [0, 1]}
    )
    assert launched.status_code == 201, launched.text
    for _ in range(3):
        supervisor.tick()
    assert (await policy.get(f"/launches/{launched.json()['id']}")).json()["status"] == "serving"
    return launched.json()["id"]


async def test_a_benchmark_stops_when_the_engine_under_it_crashes(night):
    """LLMBench cannot tell a dead engine from a slow one: the platform, which
    launched it, ends the benchmark with the launch."""
    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)
    supervisor.bench = EndlessEvaluator()
    run_id = await _serving_launch(supervisor, policy)
    bench = await policy.post(f"/launches/{run_id}/benchmarks", json={"suite": "screen"})
    bench_id = bench.json()["id"]
    supervisor.tick()
    supervisor.tick()
    assert (await policy.get(f"/benchmarks/{bench_id}")).json()["status"] == "benching"

    driver.crashed.add(f"autotune-run-{run_id}")
    supervisor.tick()
    assert (await policy.get(f"/launches/{run_id}")).json()["status"] == "failed"
    bench = (await policy.get(f"/benchmarks/{bench_id}")).json()
    assert bench["status"] == "failed"
    assert f"launch {run_id}" in bench["error"]
    assert supervisor.bench.cancelled, "the LLMBench submission was left running"


async def _benching_launch(supervisor, driver, http):
    policy = await _boot(supervisor, driver, http)
    supervisor.bench = EndlessEvaluator()
    run_id = await _serving_launch(supervisor, policy)
    bench = await policy.post(f"/launches/{run_id}/benchmarks", json={"suite": "screen"})
    for _ in range(3):
        supervisor.tick()
    assert (await policy.get(f"/launches/{run_id}")).json()["status"] == "serving"
    return policy, run_id, bench.json()["id"]


async def test_an_engine_that_died_and_was_restarted_in_place_is_a_crash(night):
    """A Deployment brings a dead engine back under the same name, so it never
    reads as crashed: the platform tells by the process it serves from."""
    supervisor, driver, http = night
    policy, run_id, bench_id = await _benching_launch(supervisor, driver, http)
    driver.restarts[f"autotune-run-{run_id}"] = 1
    supervisor.tick()
    launch = (await policy.get(f"/launches/{run_id}")).json()
    assert launch["status"] == "failed" and launch["failure_class"] == "engine_crashed"
    assert "exited while serving" in launch["error"]
    bench = (await policy.get(f"/benchmarks/{bench_id}")).json()
    assert bench["status"] == "failed" and f"launch {run_id}" in bench["error"]


async def test_an_engine_whose_pod_the_cluster_replaced_is_not_the_configs_fault(night):
    supervisor, driver, http = night
    policy, run_id, bench_id = await _benching_launch(supervisor, driver, http)
    driver.replacements[f"autotune-run-{run_id}"] = 1
    supervisor.tick()
    launch = (await policy.get(f"/launches/{run_id}")).json()
    assert launch["status"] == "failed" and launch["failure_class"] == "engine_replaced"
    assert "not to blame" in launch["error"]
    assert (await policy.get(f"/benchmarks/{bench_id}")).json()["status"] == "failed"


def test_how_an_engine_came_back_is_read_from_its_incarnations():
    from app.control.launch.failures import restart_kind

    assert restart_kind("a:0", "a:1") == "crashed"
    assert restart_kind("a:0", "b:0") == "replaced"
    assert restart_kind("a:0", "a:0,b:0") == "replaced"  # a second pod beside it
    assert restart_kind("a:2", "a:3,b:0") == "crashed"


async def test_force_stop_with_a_benchmark_running_on_a_launch_ends_both(night):
    """Stopping the launch stops its benchmark, which has a stop request of its
    own: the second stop must find it already ended, not wedge every tick."""
    from app.db.models import Run, RunStatus

    supervisor, driver, http = night
    policy, run_id, bench_id = await _benching_launch(supervisor, driver, http)
    for _ in range(2):  # the first press finalizes, the second aborts
        await _force_stop(http)
    supervisor.tick()
    supervisor.tick()  # a second tick must not trip over the first one's work
    async with http.db() as session:
        launch = await session.get(Run, run_id)
        bench = await session.get(Run, bench_id)
        assert launch.status == RunStatus.KILLED.value
        assert bench.status in (RunStatus.KILLED.value, RunStatus.FAILED.value)


async def test_releasing_an_engine_mid_benchmark_stops_its_benchmark(night):
    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)
    supervisor.bench = EndlessEvaluator()
    run_id = await _serving_launch(supervisor, policy)
    bench = await policy.post(f"/launches/{run_id}/benchmarks", json={"suite": "screen"})
    bench_id = bench.json()["id"]
    supervisor.tick()
    await http.delete(f"/api/policy/v1/launches/{run_id}", headers={"X-API-Key": policy.token})
    supervisor.tick()
    assert (await policy.get(f"/benchmarks/{bench_id}")).json()["status"] == "killed"


async def test_a_benchmark_answers_in_measures_and_the_manifest_says_what_they_mean(
    night, monkeypatch
):
    import json
    from pathlib import Path

    from app.api import policy_sessions
    from app.core.config import get_settings
    from app.evaluation import llmbench

    supervisor, driver, http = night
    replay = json.loads((Path(__file__).parent / "data/measures/replay.json").read_text())
    policy = await _boot(supervisor, driver, http)

    slug = (await policy.get("/session")).json()["services"]["benchmarks"]["screen"]["slug"]
    modules = [{
        "module_name": "replay", "order_index": 0,
        "params_json": {"concurrency": 16, "max_samples": 400, "api_key": "sk-secret"},
    }]
    monkeypatch.setattr(
        llmbench.LLMBenchClient, "list_benchmarks",
        lambda self: [{"slug": slug, "modules": modules}],
    )
    monkeypatch.setattr(policy_sessions, "_workloads", {})
    monkeypatch.setattr(get_settings(), "llmbench_replay_module", "replay")
    manifest = (await policy.get("/session")).json()
    assert {"name", "unit", "better", "meaning"} <= set(manifest["measures"][0])
    (workload,) = manifest["services"]["benchmarks"]["screen"]["workload"]
    assert workload["family"] == "replay" and "no levels" in workload["reads_as"]
    assert workload["params"] == {"concurrency": 16, "max_samples": 400}  # never a credential

    supervisor.bench = StubEvaluator(replay["metrics"])
    run_id = await _serving_launch(supervisor, policy)  # tp 2: two cards
    bench_id = (
        await policy.post(f"/launches/{run_id}/benchmarks", json={"suite": "screen"})
    ).json()["id"]
    supervisor.tick()
    supervisor.tick()
    (scenario,) = (await policy.get(f"/benchmarks/{bench_id}")).json()["measures"]
    assert scenario["kind"] == "replay" and scenario["operating_level"] is None
    assert scenario["operating"]["output_tpm_per_gpu"] == (
        replay["metrics"]["replay.output_tpm"] / 2
    )
    assert scenario["operating"]["success_rate"] == replay["metrics"]["replay.uptime"]
    assert "ttft_ms@p90[32k_64k]" in scenario["supports"]


async def test_a_whole_night_start_to_verdict(night):
    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)
    row = await _session_row(http)
    assert row.container_name == f"autotune-policy-{row.id}"
    assert policy.token not in row.launch_command  # the secret never lands in the UI
    assert "atk_[redacted]" in row.launch_command

    # The manifest: the space, the clocks, tonight's menu.
    manifest = (await policy.get("/session")).json()
    assert manifest["search_space"] == SPACE
    assert manifest["hardware"]["gpu_indices"] == list(range(8))
    assert manifest["services"]["benchmarks"]["screen"]["slug"]
    assert manifest["contenders"]["max"] == 1

    # Honesty first: the plan.
    assert (
        await policy.put(
            "/session/plan",
            {"tuning": ["tp_size"], "extras": [{"param": "enable_torch_compile",
                                               "reason": "strong prior"}]},
        )
    ).status_code == 204

    # A delegated launch on two cards; the worker brings it to SERVING.
    launched = await policy.post(
        "/launches",
        json={"engine_args": {"tp_size": 2}, "gpu_indices": [0, 1]},
        **{"Idempotency-Key": "l1"},
    )
    assert launched.status_code == 201, launched.text
    run_id = launched.json()["id"]
    assert launched.json()["config"]["enable_cache_report"] is True  # base merged in
    for _ in range(3):
        supervisor.tick()
    status = (await policy.get(f"/launches/{run_id}")).json()
    assert status["status"] == "serving"
    assert status["endpoint_url"]

    # Retrying the same Idempotency-Key returns the same run, no second engine.
    again = await policy.post(
        "/launches",
        json={"engine_args": {"tp_size": 2}, "gpu_indices": [0, 1]},
        **{"Idempotency-Key": "l1"},
    )
    assert again.json()["id"] == run_id

    # Bench it: a child EXTERNAL run, one platform-measured trial.
    bench = await policy.post(f"/launches/{run_id}/benchmarks", json={"suite": "screen"})
    assert bench.status_code == 201, bench.text
    bench_id = bench.json()["id"]
    supervisor.tick()  # health + submit
    supervisor.tick()  # poll → succeeded
    verdict = (await policy.get(f"/benchmarks/{bench_id}")).json()
    assert verdict["status"] == "succeeded"
    assert verdict["summary"]["objective_value"] == 1.0
    # ...and the launch it measured is still serving (nothing tore it down).
    assert (await policy.get(f"/launches/{run_id}")).json()["status"] == "serving"

    # Register the best-so-far, with a deviation the badge should carry.
    put = await policy.put(
        "/contenders",
        {"contenders": [{
            "rank": 1,
            "launch_spec": {
                "engine_args": {"tp_size": 2, "enable_torch_compile": True},
                "image": "sglang:test",
                "environment": {"engine_version": "0.5-test"},
            },
            "evidence": {"screen_objective": 1.0},
        }]},
    )
    assert put.status_code == 200, put.text
    assert put.json()["rejected"] == []
    [accepted] = put.json()["accepted"]
    assert [d["param"] for d in accepted["deviations"]] == ["enable_torch_compile"]

    # Release the engine — finalize is supposed to find nothing running.
    assert (await http.delete(
        f"/api/policy/v1/launches/{run_id}", headers={"X-API-Key": policy.token}
    )).status_code == 200
    supervisor.tick()
    assert (await policy.get(f"/launches/{run_id}")).json()["status"] == "released"

    # The operator's clock: shrink the window so the search deadline is past.
    async with http.db() as session:
        campaign = await session.get(Campaign, 1)
        campaign.window_end = datetime.now(UTC) + timedelta(minutes=10)
        await session.commit()
    assert (await policy.heartbeat())["command"] == "finalize"
    assert (await policy.post("/session/finalized")).status_code == 204

    # Launches are refused now — exploration is over.
    late = await policy.post(
        "/launches", json={"engine_args": {"tp_size": 1}, "gpu_indices": [3]}
    )
    assert late.status_code == 409

    supervisor.tick()  # SEARCHING -> FINALIZING
    supervisor.tick()  # FINALIZING -> VALIDATING (gracefully finalized)

    # Contenders are frozen once search is over.
    frozen = await policy.put("/contenders", {"contenders": []})
    assert frozen.status_code == 409

    # The platform validates the contender ITSELF — it is on the campaign image,
    # so the platform launches it rather than asking the policy to serve. The
    # policy is not commanded to serve; it is only kept alive, then told to exit.
    beat = await policy.heartbeat(phase="validating")
    assert beat["command"] != "serve"

    for _ in range(10):  # drive the platform-launched validation to a verdict
        supervisor.tick()
        if (await _session_row(http)).status == PolicySessionStatus.DONE.value:
            break

    row = await _session_row(http)
    assert row.status == PolicySessionStatus.DONE.value
    async with http.db() as session:
        contender = await session.get(PolicyContender, accepted["id"])
        assert contender.status == ContenderStatus.VALIDATED.value
        verdict_run = await session.get(Run, contender.run_id)
        assert verdict_run.kind == RunKind.EXPERIMENT.value  # platform-launched, not served
        assert verdict_run.status == RunStatus.SUCCEEDED.value
        machine = await session.get(Machine, 1)
        assert machine.state == MachineState.AVAILABLE.value
        campaign = await session.get(Campaign, 1)
        assert campaign.status == CampaignStatus.DONE.value  # one-off window
        key = (await session.execute(select(ApiKey))).scalars().one()
        assert key.expires_at is not None  # the token dies with the session
    # The container was torn down.
    assert row.container_name in driver.torn_down
    # 410 tells a still-running policy the night is over.
    assert (await policy.get("/session")).status_code == 410


async def test_a_dead_policy_still_gets_its_verdict(night):
    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)

    put = await policy.put(
        "/contenders",
        {"contenders": [{
            "rank": 1,
            "launch_spec": {"engine_args": {"tp_size": 2}, "image": "sglang:test"},
        }]},
    )
    assert put.status_code == 200

    # The container dies mid-search.
    row = await _session_row(http)
    driver.workload_states[row.container_name] = WorkloadState.EXITED
    supervisor.tick()
    row = await _session_row(http)
    assert row.status == PolicySessionStatus.VALIDATING.value
    assert row.failure_class == "container_died"

    # The platform launches the claim itself and measures it — and production
    # next to it, as the reference the verdict is judged against.
    supervisor.tick()  # both runs created (policy is dead, no serve command)
    async with http.db() as session:
        runs = (
            await session.execute(
                select(Run).where(Run.kind == RunKind.EXPERIMENT.value).order_by(Run.id)
            )
        ).scalars().all()
        assert [r.policy_session_id for r in runs] == [row.id, row.id]
        kinds = []
        for r in runs:
            candidate = await session.get(Candidate, r.candidate_id)
            kinds.append(candidate.is_baseline)
        assert kinds == [True, False]  # production first, then the contender
    for _ in range(4):  # launch -> ready -> health+bench -> verdict
        supervisor.tick()
    supervisor.tick()  # contender resolved; session closes

    row = await _session_row(http)
    assert row.status == PolicySessionStatus.FAILED.value  # the night records its crash
    async with http.db() as session:
        contender = (await session.execute(select(PolicyContender))).scalars().one()
        assert contender.status == ContenderStatus.VALIDATED.value  # verdict anyway


async def test_the_session_token_is_committed_before_the_container_starts(night):
    # Regression: the policy container calls GET /session within a second of
    # launch, from a different process. If its API key rode only the tick's
    # end-of-commit, a warm-cached container (the next policy on a machine)
    # beat the commit and got 401 "invalid token" before its first heartbeat.
    # The key must be durable the instant `docker run` returns.
    supervisor, driver, http = night

    seen = {}
    real_launch = driver.launch_workload

    def spy(spec):
        prefix = apikeys.split(spec.env["AUTOTUNE_API_KEY"])
        # A SEPARATE session sees only committed rows — so a hit here means the
        # key was committed before the container was started, not merely added
        # to the tick's still-open transaction.
        with supervisor.session_factory() as probe:
            seen["visible"] = probe.scalar(
                select(func.count(ApiKey.id)).where(ApiKey.prefix == prefix)
            )
        return real_launch(spec)

    driver.launch_workload = spy
    supervisor.tick()  # the machine queue reserves
    supervisor.tick()  # the pending step launches
    assert seen["visible"] == 1


async def test_policy_run_paths_stamp_a_start_time(night):
    # Regression: policy-created runs (POST /launches, and the platform-launched
    # validation) used to leave started_at NULL, unlike the classic scheduler,
    # so any duration/timeline built on it read null for the policy paths.
    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)
    launched = await policy.post(
        "/launches", json={"engine_args": {"tp_size": 2}, "gpu_indices": [0, 1]}
    )
    assert launched.status_code == 201, launched.text
    async with http.db() as session:
        run = await session.get(Run, launched.json()["id"])
        assert run.started_at is not None


async def test_re_declaring_identical_contenders_is_a_no_op(night):
    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)
    sid = (await _session_row(http)).id
    body = {"contenders": [{"rank": 1, "launch_spec": {
        "engine_args": {"tp_size": 2}, "image": "sglang:test"}}]}
    assert (await policy.put("/contenders", body)).status_code == 200
    assert (await policy.put("/contenders", body)).status_code == 200  # identical re-declare

    async with http.db() as session:
        rows = (await session.execute(
            select(PolicyContender).where(PolicyContender.session_id == sid)
        )).scalars().all()
        # The second PUT churned nothing: one registered, none superseded.
        assert sum(r.status == ContenderStatus.REGISTERED.value for r in rows) == 1
        assert sum(r.status == ContenderStatus.SUPERSEDED.value for r in rows) == 0

    # A real change still supersedes.
    changed = {"contenders": [{"rank": 1, "launch_spec": {
        "engine_args": {"tp_size": 4}, "image": "sglang:test"}}]}
    assert (await policy.put("/contenders", changed)).status_code == 200
    async with http.db() as session:
        rows = (await session.execute(
            select(PolicyContender).where(PolicyContender.session_id == sid)
        )).scalars().all()
        assert sum(r.status == ContenderStatus.SUPERSEDED.value for r in rows) == 1


async def test_a_container_death_before_heartbeat_captures_its_logs(night):
    # The bug this guards: a policy that exits before its first heartbeat was
    # recorded as `container_died` with no cause, because teardown's `docker rm`
    # destroyed the container's stdout before anyone read it. The tail must be
    # captured first, so the crash is diagnosable in the morning.
    supervisor, driver, http = night
    supervisor.tick()  # reserved in the machine queue
    supervisor.tick()  # container launched; STARTING, no heartbeat yet
    row = await _session_row(http)
    assert row.status == PolicySessionStatus.STARTING.value

    driver.workload_logs[row.container_name] = (
        "boot ok\nGET /session -> 401\nRuntimeError: session token rejected\n"
    )
    driver.workload_states[row.container_name] = WorkloadState.EXITED
    supervisor.tick()  # _advance_starting sees EXITED before first heartbeat

    row = await _session_row(http)
    assert row.status == PolicySessionStatus.VALIDATING.value
    assert row.failure_class == "container_died"
    assert "session token rejected" in row.env_snapshot["death_logs"]
    assert "last container output" in row.error
    assert "session token rejected" in row.error
    assert row.container_name in driver.torn_down  # captured, THEN torn down


async def test_the_policy_container_log_is_captured_and_served(night, monkeypatch, tmp_path):
    # The policy container's stdout+stderr is saved on teardown and served by
    # GET /policy-sessions/{id}/log, so a night is debuggable after the fact.
    from app.core.auth import create_token
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "run_log_dir", str(tmp_path))
    supervisor, driver, http = night
    await _boot(supervisor, driver, http)
    row = await _session_row(http)
    driver.workload_logs[row.container_name] = "policy boot\nsearch tick 1\n"
    driver.workload_states[row.container_name] = WorkloadState.EXITED

    for _ in range(4):  # death -> teardown captures the log -> session terminates
        supervisor.tick()
        if (await _session_row(http)).terminal:
            break
    assert (await _session_row(http)).terminal  # terminal, so the endpoint reads the file

    async with http.db() as session:
        token = create_token(await session.get(User, 1))
    resp = await http.get(
        f"/api/policy-sessions/{row.id}/log", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200, resp.text
    assert "search tick 1" in resp.text


async def test_a_running_policys_log_is_kept_when_its_pod_vanishes(night, monkeypatch, tmp_path):
    # A policy died and no log was kept: its pod was gone by the time the
    # worker read it. The worker now saves the end of a running policy's log
    # every few minutes, so a pod already gone when the death is noticed still
    # leaves its story.
    from app.control.orchestrator import container_logs
    from app.core.config import get_settings

    now = {"t": 1000.0}
    monkeypatch.setattr(container_logs.time, "monotonic", lambda: now["t"])
    monkeypatch.setattr(get_settings(), "run_log_dir", str(tmp_path))
    supervisor, driver, http = night
    await _boot(supervisor, driver, http)
    row = await _session_row(http)
    driver.workload_logs[row.container_name] = "t1: launching\nGET /launches -> 500\n"

    supervisor.tick()  # the clock starts
    now["t"] += get_settings().log_snapshot_seconds
    supervisor.tick()  # saved
    assert "GET /launches -> 500" in (await _session_row(http)).log_tail

    # Gone before the worker looks: nothing to read, and the snapshot stands in.
    driver.workload_logs[row.container_name] = ""
    driver.workload_states[row.container_name] = WorkloadState.EXITED
    supervisor.tick()
    row = await _session_row(http)
    assert row.failure_class == "container_died"
    assert "GET /launches -> 500" in row.env_snapshot["death_logs"]
    assert "GET /launches -> 500" in row.log_tail


async def test_a_graceful_exhaust_is_labelled_exhausted_not_deadline(night):
    """Regression: the winding-down heartbeats a policy sends after signalling
    `exhausted` (phase "validating", empty status) must NOT clear the standing
    signal before the worker records why search ended — otherwise the reason is
    mislabelled `deadline`."""
    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)

    # The policy says it is done, then keeps beating as it winds down.
    assert (await policy.heartbeat(status="exhausted"))["command"] == "finalize"
    await policy.heartbeat(phase="validating")  # status="" — must not erase the signal
    row = await _session_row(http)
    assert row.policy_status == "exhausted"

    supervisor.tick()  # worker records the reason
    row = await _session_row(http)
    assert row.status == PolicySessionStatus.FINALIZING.value
    assert row.search_end_reason == "policy_exhausted"


async def _recurring(http) -> None:
    """Give the campaign a daily window that is open now, so it has a next night."""
    now = datetime.now(UTC)
    async with http.db() as session:
        campaign = await session.get(Campaign, 1)
        campaign.daily_start = (now - timedelta(hours=1)).strftime("%H:%M")
        campaign.daily_end = (now + timedelta(hours=3)).strftime("%H:%M")
        campaign.schedule_timezone = "UTC"
        await session.commit()


async def _drive_to_end(supervisor, http) -> None:
    for _ in range(10):
        supervisor.tick()
        if (await _session_row(http)).terminal:
            return
    raise AssertionError("the session did not end")


async def test_an_exhausted_search_ends_a_recurring_campaign(night):
    """A policy that says it has covered its space ends the campaign, even one
    with a nightly window: the next night would only hear the same thing."""
    supervisor, driver, http = night
    await _recurring(http)
    policy = await _boot(supervisor, driver, http)

    assert (await policy.heartbeat(status="exhausted"))["command"] == "finalize"
    assert (await policy.post("/session/finalized")).status_code == 204
    await _drive_to_end(supervisor, http)

    async with http.db() as session:
        assert (await session.get(Campaign, 1)).status == CampaignStatus.DONE.value


async def test_a_deadline_leaves_a_recurring_campaign_for_its_next_night(night):
    """A search cut off by its deadline has more to do: the campaign stays active
    and its policy starts again in the next window."""
    supervisor, driver, http = night
    await _recurring(http)
    policy = await _boot(supervisor, driver, http)

    async with http.db() as session:
        campaign = await session.get(Campaign, 1)
        campaign.window_end = datetime.now(UTC) + timedelta(minutes=10)
        await session.commit()
    assert (await policy.heartbeat())["command"] == "finalize"
    assert (await policy.post("/session/finalized")).status_code == 204
    await _drive_to_end(supervisor, http)

    async with http.db() as session:
        assert (await session.get(Campaign, 1)).status == CampaignStatus.ACTIVE.value


async def test_delegated_work_after_exhausted_clears_the_signal(night):
    """The one case where a later beat *should* drop the signal: the policy
    actually resumes delegated work, proving it was not done after all."""
    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)

    await policy.heartbeat(status="exhausted")
    # A launch request is real work — it clears the done-signal.
    resp = await policy.post(
        "/launches",
        json={"engine_args": {"tp_size": 2}, "gpu_indices": [0, 1]},
        **{"Idempotency-Key": "resume-1"},
    )
    assert resp.status_code == 201, resp.text
    row = await _session_row(http)
    assert row.policy_status == ""


async def test_a_foreign_image_contender_is_skipped_not_launched(night):
    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)
    await policy.put(
        "/contenders",
        {"contenders": [{
            "rank": 1,
            "launch_spec": {"engine_args": {"tp_size": 2}, "image": "someone-elses:latest"},
        }]},
    )
    row = await _session_row(http)
    driver.workload_states[row.container_name] = WorkloadState.EXITED
    supervisor.tick()  # dead -> VALIDATING
    supervisor.tick()  # fallback refused
    async with http.db() as session:
        contender = (await session.execute(select(PolicyContender))).scalars().one()
        assert contender.status == ContenderStatus.SKIPPED.value
        assert "cannot launch" in contender.skip_reason
    supervisor.tick()
    assert (await _session_row(http)).status == PolicySessionStatus.FAILED.value


async def test_a_foreign_image_contender_is_served_by_a_live_policy(night):
    """Serve is now the exception, kept for a policy that serves its own
    engines: a contender on a DIFFERENT image, with the policy still alive, is
    asked to be served — the platform cannot launch that one itself."""
    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)
    await policy.put(
        "/contenders",
        {"contenders": [{
            "rank": 1,
            "launch_spec": {"engine_args": {"tp_size": 2}, "image": "someone-elses:latest"},
        }]},
    )
    # End search without killing the policy: push the deadline into the past.
    async with http.db() as session:
        campaign = await session.get(Campaign, 1)
        campaign.window_end = datetime.now(UTC) + timedelta(minutes=10)
        await session.commit()
    assert (await policy.heartbeat())["command"] == "finalize"
    assert (await policy.post("/session/finalized")).status_code == 204

    for _ in range(4):  # SEARCHING -> FINALIZING -> VALIDATING -> command a serve
        supervisor.tick()
    beat = await policy.heartbeat(phase="validating")
    assert beat["command"] == "serve"  # the live policy serves its own-image contender


async def test_the_allocation_is_enforced(night):
    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)

    outside = await policy.post(
        "/launches", json={"engine_args": {"tp_size": 2}, "gpu_indices": [7, 8]}
    )
    assert outside.status_code == 422
    assert "outside this session's allocation" in outside.text

    mismatched = await policy.post(
        "/launches", json={"engine_args": {"tp_size": 4}, "gpu_indices": [0, 1]}
    )
    assert mismatched.status_code == 422
    assert "must match" in mismatched.text

    placement = await policy.post(
        "/launches", json={"engine_args": {"tp_size": 2, "port": 9999}, "gpu_indices": [0, 1]}
    )
    assert placement.status_code == 422
    assert "placement" in placement.text


async def test_a_machine_with_no_cards_takes_cpu_only_launches(night):
    """A GPU-free machine gives a policy session no cards. Its launches then ask
    for none, whatever the config's parallelism; refusing them as a card-count
    mismatch left a policy nothing to launch, so it reported the space
    exhausted before trying anything (the demo on kind)."""
    supervisor, driver, http = night
    async with http.db() as session:
        (await session.get(Machine, 1)).gpu_count = 0
        await session.commit()
    policy = await _boot(supervisor, driver, http)
    assert not (await _session_row(http)).gpu_indices

    card = await policy.post("/launches", json={"engine_args": {"tp_size": 1}, "gpu_indices": [0]})
    assert card.status_code == 422
    assert "outside this session's allocation" in card.text

    launched = await policy.post(
        "/launches", json={"engine_args": {"tp_size": 1}, "gpu_indices": []}
    )
    assert launched.status_code == 201, launched.text


async def test_a_session_token_is_no_general_credential(night):
    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)
    # The lease API takes service keys — but never a session-scoped one.
    refused = await http.get(
        "/api/machines/lease", headers={"X-API-Key": policy.token}
    )
    assert refused.status_code == 401


async def test_a_general_key_cannot_speak_the_policy_api(night):
    supervisor, driver, http = night
    await _boot(supervisor, driver, http)
    secret, prefix, key_hash = apikeys.mint()
    async with http.db() as session:
        session.add(ApiKey(name="fleet", prefix=prefix, key_hash=key_hash, owner_id=1))
        await session.commit()
    refused = await http.get("/api/policy/v1/session", headers={"X-API-Key": secret})
    assert refused.status_code == 401


async def test_plan_never_touches_policy_campaigns(night):
    """The tick must not resolve campaign.planner for a policy campaign — the
    unset name would raise inside _plan, before any per-campaign guard, and
    take the whole tick down every 10 seconds."""
    supervisor, driver, http = night
    async with http.db() as session:
        campaign = await session.get(Campaign, 1)
        campaign.planner = "no-such-planner"
        await session.commit()
    supervisor.tick()  # would raise ValueError without the guard
    async with http.db() as session:
        assert (await session.execute(select(PolicySession))).scalars().one() is not None


# -- a policy that never starts, and Force stop ----------------------------------


async def _starting(supervisor, driver, http) -> PolicySession:
    supervisor.tick()  # reserved in the machine queue
    supervisor.tick()  # container launched; STARTING, no heartbeat yet
    row = await _session_row(http)
    assert row.status == PolicySessionStatus.STARTING.value
    return row


async def test_a_policy_that_can_never_start_fails_at_once(night):
    """A pod no node can ever take, or an image that will not pull, used to
    wait out the 30-minute ready timeout with no word of why."""
    supervisor, driver, http = night
    await _starting(supervisor, driver, http)

    driver.failure_reason = lambda handle: ("unschedulable", "0/9 nodes are available")
    supervisor.tick()
    assert (await _session_row(http)).status == PolicySessionStatus.STARTING.value, \
        "a busy cluster is waited for"

    driver.failure_reason = lambda handle: (
        "image_pull", "Back-off pulling image \"policy-img:1\"")
    supervisor.tick()
    row = await _session_row(http)
    assert row.failure_class == "image_pull"
    assert "cannot start" in row.error and "policy-img:1" in row.error


async def _force_stop(http):
    from app.core.auth import create_token

    async with http.db() as session:
        token = create_token(await session.get(User, 1))
    response = await http.post("/api/campaigns/1/force-stop",
                               headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200, response.text
    return response.json()


async def test_force_stop_aborts_a_policy_that_never_started(night):
    supervisor, driver, http = night
    row = await _starting(supervisor, driver, http)
    # A one-off night has nothing to resume into: Stop ends it.
    assert (await _force_stop(http))["status"] == CampaignStatus.DONE.value
    supervisor.tick()
    row = await _session_row(http)
    assert row.status == PolicySessionStatus.ABORTED.value
    assert row.container_name in driver.torn_down
    supervisor.tick()
    async with http.db() as session:
        sessions = (await session.execute(select(func.count(PolicySession.id)))).scalar()
    assert sessions == 1, "a stopped campaign starts no new session"


async def test_force_stop_finalizes_a_search_and_a_second_press_aborts(night):
    supervisor, driver, http = night
    await _boot(supervisor, driver, http)

    assert (await _force_stop(http))["status"] == CampaignStatus.ACTIVE.value
    row = await _session_row(http)
    assert row.finalize_requested_at is not None and row.abort_requested_at is None

    assert (await _force_stop(http))["status"] == CampaignStatus.DONE.value
    assert (await _session_row(http)).abort_requested_at is not None


async def test_stop_after_the_session_ended_leaves_the_campaign_done(night):
    """Abort the session, let the worker end it (a one-off night is then
    done), then press Stop: it used to reopen the campaign as paused."""
    supervisor, driver, http = night
    await _boot(supervisor, driver, http)
    async with http.db() as session:
        row = (await session.execute(select(PolicySession))).scalars().one()
        row.abort_requested_at = datetime.now(UTC)
        await session.commit()
    supervisor.tick()
    assert (await _session_row(http)).status == PolicySessionStatus.ABORTED.value
    async with http.db() as session:
        assert (await session.get(Campaign, 1)).status == CampaignStatus.DONE.value
    assert (await _force_stop(http))["status"] == CampaignStatus.DONE.value


async def test_stop_pauses_a_recurring_policy_campaign(night):
    """A nightly campaign has later nights: Stop holds it for a person."""
    supervisor, driver, http = night
    async with http.db() as session:
        campaign = await session.get(Campaign, 1)
        campaign.daily_start, campaign.daily_end = "22:00", "06:00"
        await session.commit()
    assert (await _force_stop(http))["status"] == CampaignStatus.PAUSED.value


def test_a_validation_run_takes_the_cards_its_config_uses():
    """Not the session's whole block: a tp=2 contender on an 8-card session
    asked the cluster for all 8 and waited forever for cards it never needed."""
    from types import SimpleNamespace

    from app.control.orchestrator.policy_session import _validation_cards

    session = SimpleNamespace(gpu_indices=list(range(8)))
    assert _validation_cards(session, {"tp_size": 2}, []) == [0, 1]
    # Cards a dying engine still holds go last.
    dying = [SimpleNamespace(gpu_indices=[0, 1])]
    assert _validation_cards(session, {"tp_size": 2}, dying) == [2, 3]
    assert _validation_cards(session, {"tp_size": 4, "dp_size": 2}, []) == list(range(8))
    # A card-less session (a CPU-only slice) launches with none.
    assert _validation_cards(SimpleNamespace(gpu_indices=[]), {"tp_size": 2}, []) == []


async def _validating_with(http, supervisor, driver, policy, contenders):
    put = await policy.put("/contenders", {"contenders": contenders})
    assert put.status_code == 200
    assert len(put.json()["accepted"]) == len(contenders), put.json()
    row = await _session_row(http)
    driver.workload_states[row.container_name] = WorkloadState.EXITED
    supervisor.tick()
    row = await _session_row(http)
    assert row.status == PolicySessionStatus.VALIDATING.value
    return row


async def _validation_runs(http):
    async with http.db() as session:
        return (
            await session.execute(
                select(Run).where(Run.kind == RunKind.EXPERIMENT.value).order_by(Run.id)
            )
        ).scalars().all()


_TWO = [
    {"rank": 1, "launch_spec": {"engine_args": {"tp_size": 2}, "image": "sglang:test"}},
    {"rank": 2, "launch_spec": {"engine_args": {"tp_size": 2, "chunked_prefill_size": 4096},
                                "image": "sglang:test"}},
]


async def _allow_two(http, **extra):
    async with http.db() as session:
        campaign = await session.get(Campaign, 1)
        campaign.policy_settings = {
            **(campaign.policy_settings or {}), "max_contenders": 2, **extra,
        }
        await session.commit()


async def test_validation_launches_side_by_side_on_the_gpu_budget(night):
    # Production and both contenders fit the 8-card box at once: one tick
    # starts all three, each on its own cards.
    supervisor, driver, http = night
    await _allow_two(http)
    policy = await _boot(supervisor, driver, http)
    await _validating_with(http, supervisor, driver, policy, _TWO)
    supervisor.tick()
    runs = await _validation_runs(http)
    assert len(runs) == 3
    cards = [set(r.gpu_indices or []) for r in runs]
    assert all(cards) and not (cards[0] & cards[1] or cards[0] & cards[2] or cards[1] & cards[2])
    assert len({r.service_port for r in runs}) == 3


async def test_a_small_gpu_budget_validates_one_after_another(night):
    # Two cards: production first, the contenders wait for its cards.
    supervisor, driver, http = night
    await _allow_two(http, cards=2)
    policy = await _boot(supervisor, driver, http)
    await _validating_with(http, supervisor, driver, policy, _TWO)
    supervisor.tick()
    runs = await _validation_runs(http)
    assert len(runs) == 1
    async with http.db() as session:
        assert (await session.get(Candidate, runs[0].candidate_id)).is_baseline


async def test_production_validation_can_be_turned_off(night):
    supervisor, driver, http = night
    await _allow_two(http, validate_production=False)
    policy = await _boot(supervisor, driver, http)
    await _validating_with(http, supervisor, driver, policy, _TWO)
    supervisor.tick()
    runs = await _validation_runs(http)
    assert len(runs) == 2
    async with http.db() as session:
        assert not any([(await session.get(Candidate, r.candidate_id)).is_baseline for r in runs])


async def test_re_declaring_the_same_contender_refreshes_its_evidence(night):
    # A policy measures its best again and re-declares it unchanged: no new
    # row, no supersede — but the evidence the verdict rests on is the new one.
    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)
    spec = {"engine_args": {"tp_size": 2}, "image": "sglang:test"}
    first = await policy.put("/contenders", {"contenders": [
        {"rank": 1, "launch_spec": spec, "evidence": {"values": [100.0], "measurements": 1}}]})
    again = await policy.put("/contenders", {"contenders": [
        {"rank": 1, "launch_spec": spec,
         "evidence": {"values": [100.0, 96.0], "measurements": 2}}]})
    assert first.status_code == again.status_code == 200
    assert again.json()["accepted"][0]["id"] == first.json()["accepted"][0]["id"]
    async with http.db() as session:
        rows = (await session.execute(select(PolicyContender))).scalars().all()
    assert len(rows) == 1
    assert rows[0].evidence == {"values": [100.0, 96.0], "measurements": 2}


# -- history (1.4) ------------------------------------------------------------


async def _seed_an_earlier_campaign(http) -> None:
    """Campaign 2: same owner, model and engine, one finished night with a
    digest and the platform's validation of production and one finalist.
    Campaign 3: another owner's, which no one else's policy may read."""
    from app.db.models import CandidateKind, Result

    now = datetime.now(UTC)
    async with http.db() as session:
        session.add(User(id=2, username="other", password_hash="x"))
        for cid, owner in ((2, 1), (3, 2)):
            session.add(Campaign(
                id=cid, owner_id=owner, name=f"earlier {cid}", engine="sglang",
                image="sglang:test", model_path="/models/m", served_model_name="m",
                search_space=SPACE, objective={"target_metric": "score_total"},
                status=CampaignStatus.DONE.value, machine_names=["gpu-1"], policy_id=1,
            ))
            session.add(PolicySession(
                id=100 + cid, campaign_id=cid, policy_id=1, machine_id=1,
                status=PolicySessionStatus.DONE.value, finalized_at=now,
                search_end_reason="deadline",
                digest={"summary": f"night of campaign {cid}",
                        "lessons": [{"text": "chunk 8192 is the default", "scope": "engine"}],
                        "constraints": ["chunked_prefill_size 65536 OOMs at launch"],
                        "open_questions": []},
            ))
        await session.flush()
        for n, (config, baseline, value) in enumerate((
            ({"tp_size": 2}, True, 100.0),
            ({"tp_size": 2, "page_size": 128}, False, 103.0),
            ({"tp_size": 2}, False, 108.0),  # production's own config, measured again
        )):
            candidate = Candidate(
                id=200 + n, campaign_id=2, config=config, config_hash=f"v{n}",
                kind=CandidateKind.VERIFICATION.value, is_baseline=baseline,
            )
            run = Run(
                id=300 + n, campaign_id=2, candidate_id=200 + n, machine_id=1,
                kind=RunKind.EXPERIMENT.value, status=RunStatus.SUCCEEDED.value,
                policy_session_id=102,
            )
            session.add_all([candidate, run])
            await session.flush()
            session.add(Result(
                run_id=300 + n, source="llmbench", passed=True, score=value,
                metrics={"score_total": value}, raw={}, objective_value=value, feasible=True,
            ))
        await session.commit()


async def test_history_serves_other_campaigns_findings_with_their_conditions(night):
    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)
    await _seed_an_earlier_campaign(http)
    assert (await policy.get("/session")).json()["services"]["history"] is True

    (entry,) = (await policy.get("/history")).json()  # never campaign 1, never campaign 3
    assert entry["campaign_id"] == 2 and entry["session_id"] == 102
    assert entry["digest"]["lessons"][0]["scope"] == "engine"
    assert entry["conditions"]["gpu_type"] == "H100"
    assert entry["conditions"]["objective"]["target_metric"] == "score_total"
    production = next(v for v in entry["verdicts"] if v["is_production"])
    assert production["value"] == 100.0
    finalist = next(v for v in entry["verdicts"] if v["config"].get("page_size") == 128)
    assert finalist["vs_production"] == pytest.approx(1.03) and not finalist["same_as_production"]
    # Production's config as a finalist: its ratio is the validation's noise.
    again = next(v for v in entry["verdicts"] if v["value"] == 108.0)
    assert again["same_as_production"]
    assert (await policy.get("/history?match=anything")).status_code == 422


async def test_a_night_files_its_digest_for_others(night):
    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)
    digest = {"summary": "lpm helps a little", "lessons": [{"text": "x", "scope": "model"}],
              "constraints": [], "open_questions": ["does lpm compose?"]}
    assert (await policy.put("/session/digest", digest)).status_code == 204
    row = await _session_row(http)
    assert row.digest["open_questions"] == ["does lpm compose?"] and row.digest_at
    too_long = {**digest, "summary": "x" * 20001}
    assert (await policy.put("/session/digest", too_long)).status_code == 422


async def _force_start(http, hours):
    async with http.db() as session:
        token = create_token(await session.get(User, 1))
    return await http.post("/api/campaigns/1/force-start", json={"hours": hours},
                           headers={"Authorization": f"Bearer {token}"})


async def test_force_start_refuses_a_window_validation_would_fill(night):
    """Before any run has come up, each validation launch is budgeted the whole
    model load limit (2 h), so a 4 h start held back more than it had: the
    policy was finalized as it started and the campaign ended done, with
    nothing run. Refused now, with what the hold-back is made of."""
    _, _, http = night
    async with http.db() as session:
        campaign = await session.get(Campaign, 1)
        campaign.status = CampaignStatus.DRAFT.value
        campaign.policy_settings = {"max_contenders": 1}
        timing.set_priors(campaign, {"screen": 30})
        await session.commit()

    refused = await _force_start(http, 4)
    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert "a 4 h window leaves no time to search" in detail
    assert "the model load limit" in detail and "at least 7 h" in detail
    async with http.db() as session:
        assert (await session.get(Campaign, 1)).status == CampaignStatus.DRAFT.value

    assert (await _force_start(http, 7)).status_code == 200


async def test_force_start_takes_a_startup_the_campaign_states(night):
    _, _, http = night
    async with http.db() as session:
        campaign = await session.get(Campaign, 1)
        campaign.policy_settings = {"max_contenders": 1, "model_startup_minutes": 45}
        timing.set_priors(campaign, {"screen": 30})
        await session.commit()
    assert (await _force_start(http, 4)).status_code == 200
