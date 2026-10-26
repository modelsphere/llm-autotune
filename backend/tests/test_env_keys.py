"""Contract 1.5: environment variables as `env.<NAME>` config keys.

A config stays one flat dict of scalars; the platform splits the env keys off
at the launch boundary into the engine container's environment, keeps them
off the command line, and carries them into a winner's deployable config.
"""

from types import SimpleNamespace

from app.control.engines import vocabulary as vocab_mod
from app.control.engines.base import render_args
from app.control.engines.env_keys import env_problems, split_env
from app.control.launch_config import LaunchConfig
from app.control.search.validation import ValidationContext, validate_config
from app.db.models import Campaign, EngineVocabulary
from tests.test_policy_sessions import _boot, night  # noqa: F401  (night is a fixture)

ENTRIES = [
    {"kind": "env", "name": "SGLANG_ENABLE_OVERLAP_PLAN_STREAM", "type": "bool", "default": False,
     "help": "Plan the next step on a separate stream (Overlap Spec V2).", "help_source": "docs"},
    {"kind": "env", "name": "SGLANG_USE_1STAGE_ALLREDUCE", "type": "bool", "default": False,
     "help": "1-stage all-reduce kernel.", "help_source": "docs"},
    {"kind": "env", "name": "SGLANG_LOG_GC", "type": "bool", "default": False},
    {"kind": "flag", "name": "chunked_prefill_size", "type": "int", "default": None,
     "help": "Tokens per prefill chunk.", "help_source": "engine"},
]
LABELS = (
    vocab_mod.Label("env", "SGLANG_ENABLE_OVERLAP_PLAN_STREAM", "high", when="speculative decoding on"),
    vocab_mod.Label("env", "SGLANG_USE_1STAGE_ALLREDUCE", "mid", hardware=("amd",)),
    vocab_mod.Label("flag", "chunked_prefill_size", "mid"),
    vocab_mod.Label("env", "SGLANG_NOT_IN_THIS_IMAGE", "high"),
)


async def _store_vocabulary(http, image="sglang:test"):
    async with http.db() as session:
        session.add(EngineVocabulary(
            engine="sglang", image=image, image_digest="sha256:abc", engine_version="0.5.19",
            status="ok", entries=ENTRIES, notes=[],
        ))
        await session.commit()


def test_env_keys_split_off_and_render_as_the_engines_read_them():
    args, env = split_env({
        "tp": 2, "env.SGLANG_ENABLE_OVERLAP_PLAN_STREAM": True, "env.SGLANG_OFF": False,
        "env.SGLANG_INIT_NEW_TOKEN_RATIO": 0.5, "env.VLLM_ALL2ALL_BACKEND": "deepep",
    })
    assert args == {"tp": 2}
    # Booleans as 1/0: vllm reads several of its switches with int().
    assert env == {
        "SGLANG_ENABLE_OVERLAP_PLAN_STREAM": "1", "SGLANG_OFF": "0",
        "SGLANG_INIT_NEW_TOKEN_RATIO": "0.5", "VLLM_ALL2ALL_BACKEND": "deepep",
    }


def test_env_keys_never_reach_the_command_line():
    argv = render_args({"tp": 2, "env.SGLANG_ENABLE_OVERLAP_PLAN_STREAM": True}, "sglang")
    assert argv == ["--tp-size", "2"]


def test_placement_credentials_and_bad_names_are_refused():
    problems = env_problems({
        "env.CUDA_VISIBLE_DEVICES": "0,1", "env.HF_TOKEN": "x", "env.VLLM_PORT": 9,
        "env.lower_case": 1, "env.SGLANG_LIST": [1, 2], "env.SGLANG_EMPTY": " ",
        # A tuning knob that merely mentions tokens is not a credential.
        "env.SGLANG_CLIP_MAX_NEW_TOKENS_ESTIMATION": 2048, "tp": 2,
    })
    assert {p["param"]: p["kind"] for p in problems} == {
        "env.CUDA_VISIBLE_DEVICES": "placement",
        "env.HF_TOKEN": "placement",
        "env.VLLM_PORT": "placement",
        "env.lower_case": "bad_key",
        "env.SGLANG_LIST": "not_renderable",
        "env.SGLANG_EMPTY": "not_renderable",
    }


def test_the_platforms_own_search_refuses_placement_env_too():
    ctx = ValidationContext(gpu_count=8, engine="sglang")
    assert "placement" in validate_config({"env.CUDA_VISIBLE_DEVICES": "0"}, ctx)
    assert validate_config({"tp": 2, "env.SGLANG_ENABLE_OVERLAP_PLAN_STREAM": True}, ctx) is None


def test_a_winners_env_keys_land_in_its_deployable_env():
    campaign = SimpleNamespace(
        engine="sglang", image="sglang:test", model_path="/models/m",
        served_model_name="m", service_port=28200, search_space={},
        extra_env={"SGLANG_A": "campaign", "NCCL_DEBUG": "WARN"}, extra_volumes={},
    )
    cfg = LaunchConfig.from_campaign(
        campaign, engine_args={"tp": 2, "env.SGLANG_A": "config", "env.SGLANG_B": True}
    )
    assert cfg.engine_args == {"tp": 2}
    assert cfg.extra_env == {"SGLANG_A": "config", "NCCL_DEBUG": "WARN", "SGLANG_B": "1"}


# -- over the API ------------------------------------------------------------------------


async def test_check_launch_and_the_container_env(night):  # noqa: F811
    supervisor, driver, http = night
    specs = []
    launch = driver.launch
    driver.launch = lambda spec: (specs.append(spec), launch(spec))[1]
    await _store_vocabulary(http)
    policy = await _boot(supervisor, driver, http)
    assert (await policy.get("/session")).json()["services"]["env_keys"] is True

    config = {"tp_size": 2, "env.SGLANG_ENABLE_OVERLAP_PLAN_STREAM": True}
    checked = (await policy.post("/configs/check", json={"engine_args": config})).json()
    assert checked["ok"] is True, checked["problems"]
    assert checked["env"] == {"SGLANG_ENABLE_OVERLAP_PLAN_STREAM": "1"}
    assert not any(a.startswith("--env") for a in checked["command"])

    bad = await policy.post("/configs/check", json={"engine_args": {
        "tp_size": 2, "env.CUDA_VISIBLE_DEVICES": "3"}})
    assert bad.json()["ok"] is False
    assert bad.json()["problems"][0]["kind"] == "placement"
    refused = await policy.post("/launches", json={
        "engine_args": {"tp_size": 2, "env.CUDA_VISIBLE_DEVICES": "3"}, "gpu_indices": [0, 1]})
    assert refused.status_code == 422

    launched = await policy.post("/launches", json={"engine_args": config, "gpu_indices": [0, 1]})
    assert launched.status_code == 201, launched.text
    assert launched.json()["config"]["env.SGLANG_ENABLE_OVERLAP_PLAN_STREAM"] is True
    for _ in range(3):
        supervisor.tick()
    [spec] = [s for s in specs if s.run_id == launched.json()["id"]]
    assert spec.env["SGLANG_ENABLE_OVERLAP_PLAN_STREAM"] == "1"
    assert not any(k.startswith("env.") for k in spec.engine_args)


def test_labels_rank_entries_and_drop_what_does_not_apply(monkeypatch):
    monkeypatch.setattr(vocab_mod, "load_labels", lambda engine: LABELS)
    vocab = vocab_mod.Vocabulary("sglang", "0.5.19", [
        vocab_mod.Entry(**{**e, "choices": ()}) for e in ENTRIES
    ])
    rows = vocab_mod.annotate(vocab, "H100")
    # The AMD-only switch is gone on an H100; a label for a switch the image
    # lacks never appears; unlabeled entries are "unknown", last.
    assert [(r["name"], r["level"]) for r in rows] == [
        ("SGLANG_ENABLE_OVERLAP_PLAN_STREAM", "high"),
        ("chunked_prefill_size", "mid"),
        ("SGLANG_LOG_GC", "unknown"),
    ]
    assert rows[0]["when"] == "speculative decoding on"
    assert "SGLANG_USE_1STAGE_ALLREDUCE" in {
        r["name"] for r in vocab_mod.annotate(vocab, "")  # unknown card keeps it
    }
    assert [r["name"] for r in vocab_mod.lookup(rows, kind="env", level="mid")] == [
        "SGLANG_ENABLE_OVERLAP_PLAN_STREAM"
    ]
    assert [r["name"] for r in vocab_mod.lookup(rows, query="prefill")] == [
        "chunked_prefill_size"
    ]


def test_the_shipped_label_files_load():
    for engine in ("sglang", "vllm"):
        vocab_mod.load_labels.cache_clear()
        vocab_mod.load_labels(engine)


async def test_an_env_var_the_image_does_not_read_is_refused_with_the_near_names(night):  # noqa: F811
    supervisor, driver, http = night
    await _store_vocabulary(http)
    policy = await _boot(supervisor, driver, http)
    body = (await policy.post("/configs/check", json={"engine_args": {
        "tp_size": 2, "env.SGLANG_ENABLE_OVERLAP_PLAN_STREEM": True}})).json()
    assert body["ok"] is False
    [problem] = body["problems"]
    assert problem["kind"] == "unknown_env"
    assert "env.SGLANG_ENABLE_OVERLAP_PLAN_STREAM" in problem["message"]


async def test_without_a_vocabulary_env_keys_are_refused_unless_the_campaign_says_warn(night):  # noqa: F811
    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)
    config = {"tp_size": 2, "env.SGLANG_ENABLE_OVERLAP_PLAN_STREAM": True}
    refused = (await policy.post("/configs/check", json={"engine_args": config})).json()
    assert refused["ok"] is False and refused["problems"][0]["kind"] == "unknown_env"

    async with http.db() as session:
        campaign = await session.get(Campaign, 1)
        campaign.policy_settings = {**campaign.policy_settings, "env_without_vocabulary": "warn"}
        await session.commit()
    allowed = (await policy.post("/configs/check", json={"engine_args": config})).json()
    assert allowed["ok"] is True, allowed["problems"]


async def test_the_vocabulary_endpoint(night, monkeypatch):  # noqa: F811
    supervisor, driver, http = night
    monkeypatch.setattr(vocab_mod, "load_labels", lambda engine: LABELS)
    policy = await _boot(supervisor, driver, http)
    assert (await policy.get("/engine/vocabulary")).json()["status"] == "none"

    await _store_vocabulary(http)
    body = (await policy.get("/engine/vocabulary?kind=env")).json()
    assert body["status"] == "ok" and body["engine_version"] == "0.5.19"
    # The fixture's machine is an H100: the AMD-only switch is left out.
    assert [e["name"] for e in body["entries"]] == ["SGLANG_ENABLE_OVERLAP_PLAN_STREAM", "SGLANG_LOG_GC"]
    high = (await policy.get("/engine/vocabulary?level=high")).json()
    assert [e["name"] for e in high["entries"]] == ["SGLANG_ENABLE_OVERLAP_PLAN_STREAM"]
