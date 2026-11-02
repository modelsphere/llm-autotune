"""Contract 1.3: what a policy can read about its own night, and what it may
not change.

Diagnostics re-expose to the session token what the platform already records
(engine logs, structured results, run environments); the dry run and the
refusals share one definition of a launchable config: no placement, no
forbidden parameter changed, a command line that reads back as the same
config, and the safety checks.
"""

from app.control.engine_command import round_trip_problems
from app.control.search.space import DEFAULT_FORBIDDEN_PARAMS, forbidden_hits
from app.db.models import Campaign, Run
from tests.test_policy_sessions import _boot, night  # noqa: F401  (night is a fixture)

# -- forbidden parameters ---------------------------------------------------------


def test_a_forbidden_parameter_may_keep_the_campaigns_value_but_not_change_it():
    space = {"base": {"kv_cache_dtype": "fp8_e4m3"}, "grid": {"tp_size": [1, 2]}}
    unchanged = {"kv_cache_dtype": "fp8_e4m3", "tp_size": 2}
    assert forbidden_hits(unchanged, space, DEFAULT_FORBIDDEN_PARAMS, "sglang") == []

    changed = {"kv_cache_dtype": "auto", "tp_size": 2}
    [hit] = forbidden_hits(changed, space, DEFAULT_FORBIDDEN_PARAMS, "sglang")
    assert hit["param"] == "kv_cache_dtype"

    introduced = {"quantization": "fp8", "tool_call_parser": "qwen25", "tp_size": 2}
    found = forbidden_hits(introduced, space, DEFAULT_FORBIDDEN_PARAMS, "sglang")
    assert sorted(h["param"] for h in found) == ["quantization", "tool_call_parser"]
    assert {h["pattern"] for h in found} == {"quantization*", "*_parser"}


def test_a_parameter_the_campaign_sweeps_is_never_forbidden():
    space = {"grid": {"kv_cache_dtype": ["auto", "fp8_e4m3"]}}
    assert forbidden_hits({"kv_cache_dtype": "fp8_e4m3"}, space, ["kv_cache_dtype"]) == []


def test_an_empty_list_forbids_nothing():
    assert forbidden_hits({"quantization": "fp8"}, {}, []) == []


# -- the command-line round trip ----------------------------------------------------


def test_ordinary_configs_round_trip_on_both_engines():
    assert round_trip_problems(
        {"tp": 2, "chunked_prefill_size": 4096, "mem_fraction_static": 0.85,
         "enable_torch_compile": True, "disable_radix_cache": False,
         "schedule_policy": "lpm"},
        "sglang",
    ) == []
    assert round_trip_problems(
        {"tp": 2, "gpu_memory_utilization": 0.9, "enable_prefix_caching": False,
         "a_flag_the_catalog_does_not_know": False,
         "speculative_config": '{"method": "eagle", "num_speculative_tokens": 3}'},
        "vllm",
    ) == []


def test_values_a_command_line_cannot_carry_are_refused():
    problems = round_trip_problems(
        {"cuda_graph_bs": [1, 2, 4], "speculative_config": {"method": "eagle"},
         "tokenizer_mode": "", "weird": "--oops", "Bad-Key": 1},
        "sglang",
    )
    assert {p["param"]: p["kind"] for p in problems} == {
        "cuda_graph_bs": "not_renderable",
        "speculative_config": "not_renderable",
        "tokenizer_mode": "not_renderable",
        "weird": "not_renderable",
        "Bad-Key": "bad_key",
    }


# -- over the API ------------------------------------------------------------------------


async def test_the_manifest_carries_the_forbidden_list_params_and_diagnostics(night):  # noqa: F811
    supervisor, driver, http = night
    async with http.db() as session:
        campaign = await session.get(Campaign, 1)
        campaign.policy_settings = {
            **campaign.policy_settings, "params": {"model": "some-llm", "effort": "high"},
        }
        await session.commit()
    policy = await _boot(supervisor, driver, http)
    manifest = (await policy.get("/session")).json()
    version = tuple(int(p) for p in manifest["contract"]["version"].split("."))
    assert version >= (1, 3)
    assert manifest["services"]["diagnostics"] is True
    assert manifest["policy"]["params"] == {"model": "some-llm", "effort": "high"}
    assert manifest["space_policy"]["forbidden"] == list(DEFAULT_FORBIDDEN_PARAMS)


async def test_check_answers_without_refusing_and_launches_refuse_the_same(night):  # noqa: F811
    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)

    clean = (await policy.post("/configs/check", json={"engine_args": {"tp_size": 2}})).json()
    assert clean["ok"] is True
    assert clean["cards"] == 2
    assert clean["config"]["enable_cache_report"] is True  # base merged in
    assert "--tp-size" in clean["command"] and "2" in clean["command"]

    bad = {"tp_size": 2, "quantization": "fp8", "cuda_graph_bs": [1, 2], "port": 1}
    checked = await policy.post("/configs/check", json={"engine_args": bad})
    assert checked.status_code == 200
    body = checked.json()
    assert body["ok"] is False
    assert {p["kind"] for p in body["problems"]} == {"placement", "forbidden", "not_renderable"}

    refused = await policy.post("/launches", json={"engine_args": bad, "gpu_indices": [0, 1]})
    assert refused.status_code == 422
    for word in ("placement", "forbidden", "a list"):
        assert word in refused.text

    contenders = await policy.put("/contenders", {"contenders": [{
        "rank": 1,
        "launch_spec": {"engine_args": {"tp_size": 2, "kv_cache_dtype": "fp8_e5m2"},
                        "image": "sglang:test"},
    }]})
    assert contenders.status_code == 200
    assert contenders.json()["accepted"] == []
    assert "forbidden" in contenders.json()["rejected"][0]["reason"]


async def test_catalog_warnings_name_only_what_the_policy_chose(night):  # noqa: F811
    supervisor, driver, http = night
    async with http.db() as session:
        campaign = await session.get(Campaign, 1)
        space = dict(campaign.search_space)
        space["base"] = {**space.get("base", {}), "operator_knob": 3}
        campaign.search_space = space
        await session.commit()
    policy = await _boot(supervisor, driver, http)

    def warned(args):
        return policy.post("/configs/check", json={"engine_args": args})

    kept = (await warned({"tp_size": 2})).json()["warnings"]
    assert not any("operator_knob" in w for w in kept)
    args = {"tp_size": 2, "operator_knob": 4, "mat_fraction_static": 0.85}
    changed = (await warned(args)).json()["warnings"]
    assert any(w.startswith("operator_knob ") for w in changed)
    assert any(w.startswith("mat_fraction_static ") for w in changed)


async def test_a_campaign_can_lift_the_forbidden_list(night):  # noqa: F811
    supervisor, driver, http = night
    async with http.db() as session:
        campaign = await session.get(Campaign, 1)
        campaign.policy_settings = {**campaign.policy_settings, "forbidden_params": []}
        await session.commit()
    policy = await _boot(supervisor, driver, http)
    launched = await policy.post(
        "/launches",
        json={"engine_args": {"tp_size": 2, "kv_cache_dtype": "fp8_e4m3"}, "gpu_indices": [0, 1]},
    )
    assert launched.status_code == 201, launched.text


async def test_logs_environment_and_full_results_of_a_launch(night):  # noqa: F811
    supervisor, driver, http = night
    driver._env = {"engine_version": "0.5-test", "image_digest": "sglang@sha256:abc"}
    policy = await _boot(supervisor, driver, http)
    run_id = (await policy.post(
        "/launches", json={"engine_args": {"tp_size": 2}, "gpu_indices": [0, 1]}
    )).json()["id"]
    for _ in range(3):
        supervisor.tick()
    assert (await policy.get(f"/launches/{run_id}")).json()["status"] == "serving"

    env = (await policy.get(f"/launches/{run_id}/environment")).json()
    assert env["engine_version"] == "0.5-test"
    assert env["image_digest"] == "sglang@sha256:abc"

    async with http.db() as session:
        run = await session.get(Run, run_id)
        run.status = "succeeded"  # a finished run: its saved copy is what is read
        run.log_tail = "\n".join(
            ["loading weights", "Decode batch. #running-req: 12, token usage: 0.41",
             "warmup done", "torch.OutOfMemoryError: CUDA out of memory", "exiting"]
        )
        await session.commit()
    tail = (await policy.get(f"/launches/{run_id}/log?tail=2")).text
    assert tail == "torch.OutOfMemoryError: CUDA out of memory\nexiting"
    grepped = (await policy.get(f"/launches/{run_id}/log?grep=out%20of%20memory&context=1")).text
    assert grepped.splitlines() == [
        "warmup done", "torch.OutOfMemoryError: CUDA out of memory", "exiting",
    ]


async def test_the_full_results_of_a_benchmark(night):  # noqa: F811
    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)
    run_id = (await policy.post(
        "/launches", json={"engine_args": {"tp_size": 2}, "gpu_indices": [0, 1]}
    )).json()["id"]
    for _ in range(3):
        supervisor.tick()
    bench_id = (await policy.post(
        f"/launches/{run_id}/benchmarks", json={"suite": "screen"}
    )).json()["id"]

    early = await policy.get(f"/benchmarks/{bench_id}/results/raw")
    assert early.status_code == 409

    supervisor.tick()
    supervisor.tick()
    results = (await policy.get(f"/benchmarks/{bench_id}/results")).json()
    assert results["status"] == "measured"
    assert results["objective_value"] == 1.0
    assert results["metrics"]["score_total"] == 1.0
    assert (await policy.get(f"/benchmarks/{bench_id}/results/raw")).status_code == 200

    # Another session's run, or the wrong kind, is not this session's to read.
    assert (await policy.get(f"/launches/{bench_id}/log")).status_code == 404
    assert (await policy.get(f"/benchmarks/{run_id}/results")).status_code == 404


async def test_a_refused_config_is_kept_in_the_activity_log(night):  # noqa: F811
    from sqlalchemy import select

    from app.db.models import Event
    from app.reporting.activity import describe

    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)
    refused = await policy.post(
        "/launches", json={"engine_args": {"tp_size": 2, "dtype": "float16"}, "gpu_indices": [0, 1]}
    )
    assert refused.status_code == 422
    async with http.db() as session:
        [event] = (await session.execute(
            select(Event).where(Event.kind == "policy_config_refused")
        )).scalars().all()
    assert "dtype is forbidden" in describe(event)


# -- secret env: what an agent policy's LLM key needs ---------------------------------


async def test_secret_env_is_write_only_and_kept_out_of_the_launch_command(night):  # noqa: F811
    from app.core.auth import create_token
    from app.db.models import Policy, PolicySession, User

    supervisor, driver, http = night
    async with http.db() as session:
        token = create_token(await session.get(User, 1))
    auth = {"Authorization": f"Bearer {token}"}

    body = {"name": "tpe-test", "image": "policy-img:1", "ports": 2,
            "env": {"LLM_MODEL": "some-llm"},
            "secret_env": {"LLM_API_KEY": "sk-very-secret-123"}}
    updated = await http.put("/api/policies/1", json=body, headers=auth)
    assert updated.status_code == 200, updated.text
    assert updated.json()["secret_env_keys"] == ["LLM_API_KEY"]
    assert "sk-very-secret-123" not in updated.text
    listed = await http.get("/api/policies", headers=auth)
    assert "sk-very-secret-123" not in listed.text

    # A blank value keeps the stored secret; omitting secret_env keeps them all.
    kept = await http.put(
        "/api/policies/1", json={**body, "secret_env": {"LLM_API_KEY": ""}}, headers=auth
    )
    assert kept.json()["secret_env_keys"] == ["LLM_API_KEY"]
    untouched = {k: v for k, v in body.items() if k != "secret_env"}
    assert (await http.put("/api/policies/1", json=untouched, headers=auth)).json()[
        "secret_env_keys"] == ["LLM_API_KEY"]
    async with http.db() as session:
        row = await session.get(Policy, 1)
        assert "sk-very-secret-123" not in row.secret_env  # encrypted at rest
        assert row.secret_env_values() == {"LLM_API_KEY": "sk-very-secret-123"}

    await _boot(supervisor, driver, http)
    assert driver.workloads[0].env["LLM_API_KEY"] == "sk-very-secret-123"
    assert driver.workloads[0].env["LLM_MODEL"] == "some-llm"
    async with http.db() as session:
        from sqlalchemy import select

        row = (await session.execute(select(PolicySession))).scalars().one()
        launch_command = row.launch_command
    assert "sk-very-secret-123" not in launch_command
    assert "[LLM_API_KEY redacted]" in launch_command
    assert "LLM_MODEL=some-llm" in launch_command

    cleared = await http.put("/api/policies/1", json={**body, "secret_env": {}}, headers=auth)
    assert cleared.json()["secret_env_keys"] == []
