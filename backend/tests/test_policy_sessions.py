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
import pytest_asyncio
from sqlalchemy import create_engine, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.control.launch.base import WorkloadState
from app.control.orchestrator.supervisor import Supervisor
from app.control.search.space import deviations
from app.core import apikeys
from app.db.base import Base, get_async_session
from app.db.models import (
    ApiKey,
    BaselineStatus,
    Campaign,
    CampaignStatus,
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
from tests.fakes import CompletingDriver, StubEvaluator

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
                baseline_status=BaselineStatus.CLEARED.value,
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
    """tick → container launched; first heartbeat; tick → SEARCHING."""
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

    # The platform launches the claim itself and measures it.
    supervisor.tick()  # fallback run created (policy is dead, no serve command)
    async with http.db() as session:
        fallback = (
            await session.execute(select(Run).where(Run.kind == RunKind.EXPERIMENT.value))
        ).scalars().one()
        assert fallback.policy_session_id == row.id
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
    supervisor.tick()
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
