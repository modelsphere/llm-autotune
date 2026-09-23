"""Agent API: the parts, documents and comparison an LLM writes a report from.

Seeds one campaign with five runs — A (the baseline config), B, C, D, E —
each with a benchmark result. A report picks A as baseline and C and D as
attempts; B and E must not appear anywhere in that comparison.
"""

import base64
import copy
import json
from itertools import count

import httpx
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.auth import create_token
from app.db.base import Base, get_async_session
from app.db.models import Campaign, Candidate, Machine, Result, Run, User
from app.evaluation.llmbench import _flatten_metrics, module_reports
from app.main import app

_counter = count()

IMAGE = "registry.example.com/sglang:v0.5.15-cu129"
SWEEP = "perf_guidellm_sweep"
SLUG = "bh-glm-h100"

SWEEP_PARAMS = {
    "input_tokens": 8000, "output_tokens": 1000, "concurrencies": "1,2,4,8",
    "requests_per_concurrency": 20, "slo_max_ttft_ms": 60000, "slo_ttft_percentile": "p50",
    "slo_min_request_output_tps": 15,
}


def _levels(base_tps: float) -> dict:
    """A sweep that scales with concurrency, TTFT climbing with it."""
    out = {}
    for c in (1, 2, 4, 8):
        out[f"c{c}"] = {
            "total_tps_mean": round(base_tps * c, 1),
            # c8 misses the SLO: the best level is c4, not the reported c8
            "meets_slo": 1.0 if c <= 4 else 0.0,
            "output_tps": round(base_tps * c * 0.9, 1),
            "request_output_tps": round(base_tps * 0.9, 1),
            "ttft_p50_ms": 300 + 40 * c,
            "ttft_p99_ms": 500 + 80 * c,
            "tpot_p50_ms": 15.0,
            "http_status_200": 20,
        }
    return out


OBJECTIVE = {
    "target_metric": f"{SWEEP}.output_tpm_card_norm",
    "redlines": [
        {"metric": f"{SWEEP}.ttft_p50_ms", "op": "<=", "value": 60000},
        {"metric": f"{SWEEP}.request_output_tps", "op": ">=", "value": 15},
        {"metric": "opencompass.gsm8k", "op": ">=", "value": 0.9},
    ],
}

REPLAY_PARAMS = {
    "concurrency": 8, "max_samples": 200, "dataset_source": "auto",
    "dataset_profile": "prod-traffic-sample", "judge_enable": False,
}


def _replay_run(base_tps: float, *, concurrency: int = 8) -> dict:
    """A traffic replay: flat metrics, one concurrency, no levels. Each run
    replays its own pin file of the same build, as LLMBench does."""
    tpm = base_tps * 1000
    return {
        "module_name": "replay", "status": "done", "passed": True, "score": tpm,
        "order_index": 1, "weight": 1.0,
        "params_json": {
            **REPLAY_PARAMS, "concurrency": concurrency,
            "dataset_path": f"/pins/s{int(base_tps)}-c{concurrency}__20260830T145721Z.jsonl.gz",
        },
        "metrics_json": {
            "score_card_norm": tpm * 4, "total_tpm_card_norm": tpm * 40,
            "output_tpm_card_norm": tpm * 2, "cached_tpm": tpm * 5, "cache_hit_rate": 0.4,
            "output_tps_mean": base_tps / 2, "ttft_p50_ms": 900.0, "ttft_p99_ms": 4000.0,
            "ttft_16k_32k_p50_ms": 1200.0, "ttft_16k_32k_count": 150,
            "ttft_ge_256k_p50_ms": None, "ttft_ge_256k_count": 0,
            "uptime": 1.0, "unfinished_rate": 0.0, "total_requests": 200,
            "dataset_id": "20260830T145721Z",
            "finish_marker_counts": {"openai_finish_reason_stop": 190},
        },
        "metric_configs_json": [
            {"key": "uptime", "role": "redline", "min_val": 0.99, "max_val": None},
            {"key": "cache_hit_rate", "role": "redline", "min_val": 0.3, "max_val": None},
            {"key": "score_card_norm", "role": "display"},
        ],
    }


def _raw(base_tps: float, *, passed: bool = True, gsm8k: float = 0.93) -> dict:
    levels = _levels(base_tps)
    quality_ok = gsm8k >= 0.6  # the benchmark's own redline on gsm8k
    return {
        "status": "done", "passed": passed and quality_ok, "score_total": 1.4,
        "benchmark_config_hash": "abc12345",
        "runs": [
            {
                "module_name": SWEEP, "status": "done", "passed": passed, "score": 1.4,
                "order_index": 0, "weight": 1.0,
                "params_json": SWEEP_PARAMS,
                "metrics_json": {
                    "output_tps": levels["c8"]["output_tps"],
                    "request_output_tps": levels["c8"]["request_output_tps"],
                    "reported_concurrency": 8,
                    "reported_level_meets_slo": passed,
                    "peak_concurrency": 8,
                    "ttft_p50_ms": levels["c8"]["ttft_p50_ms"],
                    "ttft_p99_ms": levels["c8"]["ttft_p99_ms"],
                    "total_tpm_card_norm": round(base_tps * 8 * 60 / 2, 1),
                    "output_tpm_card_norm": round(base_tps * 8 * 0.9 * 60 / 2, 1),
                    "uptime": 1.0,
                    **levels,
                },
                "metric_configs_json": [
                    {"key": "uptime", "role": "redline", "min_val": 0.99, "max_val": None},
                    {"key": "ttft_p99_ms", "role": "redline", "min_val": None, "max_val": 120000},
                    {"key": "output_tps", "role": "score", "weight": 1},
                ],
            },
            {
                "module_name": "opencompass", "status": "done", "passed": quality_ok,
                "score": 0.9,
                "order_index": 1, "weight": 0.0,
                "params_json": {"datasets": ["gsm8k"]},
                "metrics_json": {"gsm8k": gsm8k},
                "metric_configs_json": [
                    {"key": "gsm8k", "role": "redline", "min_val": 0.6, "max_val": None},
                ],
            },
        ],
    }


@pytest_asyncio.fixture
async def stack():
    name = f"ag_{next(_counter)}"
    db = f"sqlite+aiosqlite:///file:{name}?mode=memory&cache=shared&uri=true"
    engine = create_async_engine(db)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _session():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_async_session] = _session
    async with factory() as s:
        s.add(User(id=1, username="admin", password_hash="x", role="admin"))
        s.add(Machine(id=1, name="node-5", host="10.0.0.5", gpu_type="H100", gpu_count=8))
        s.add(Campaign(
            id=1, owner_id=1, name="GLM · H100", engine="sglang", image=IMAGE,
            model_path="/mnt/models/GLM-5.2", served_model_name="glm-5.2",
            search_space={"base": {"tp": 2}}, benchmark_slug=SLUG,
            objective=OBJECTIVE, policy_id=None,
        ))
        await s.commit()
        token = create_token(await s.get(User, 1))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
        http.headers["Authorization"] = f"Bearer {token}"
        yield http, factory

    app.dependency_overrides.clear()
    await engine.dispose()


async def _seed_run(
    factory, *, sid: int, name: str, engine_args: dict, base_tps: float,
    notes: str = "", baseline: bool = False, failed: bool = False,
    running: bool = False, params: dict | None = None,
    gsm8k: float = 0.93, replay_concurrency: int | None = None,
) -> int:
    """One run of the campaign: a candidate, a run, and its result."""
    run_id = 100 + sid
    async with factory() as s:
        s.add(Candidate(
            id=run_id, campaign_id=1, config=engine_args, config_hash=f"h{sid}",
            is_baseline=baseline,
        ))
        status = "launching" if running else ("failed" if failed else "succeeded")
        s.add(Run(
            id=run_id, campaign_id=1, candidate_id=run_id, machine_id=1, status=status,
            gpu_indices=[0, 1], service_port=28200,
            launch_command=(
                "" if running else f"docker run -d --name autotune-run-{run_id} {IMAGE} …"
            ),
            env_snapshot={} if running else {
                "engine_version": "0.5.15", "torch_version": "2.9.0", "cuda_version": "12.9",
                "gpu_name": "NVIDIA H100", "card_type": "H100", "image_digest": "sha256:feed",
            },
            failure_class="engine_crash" if failed else "",
            error="CUDA out of memory\nmore lines" if failed else "",
        ))
        if not failed and not running:
            raw = _raw(base_tps, gsm8k=gsm8k)
            if params:
                raw["runs"][0]["params_json"] = {**SWEEP_PARAMS, **params}
            if replay_concurrency is not None:
                raw["runs"].insert(1, _replay_run(base_tps, concurrency=replay_concurrency))
                raw["runs"][2]["order_index"] = 2
            metrics = _flatten_metrics(raw)
            s.add(Result(
                run_id=run_id, source="llmbench", passed=raw["passed"], score=1.4, metrics=metrics,
                raw=raw, objective_value=metrics[f"{SWEEP}.output_tpm_card_norm"], feasible=True,
            ))
        await s.commit()
    return run_id


async def _seed_five(factory) -> dict[str, int]:
    """A baseline, C and D, and the B and E a report leaves out."""
    return {
        "A": await _seed_run(factory, sid=1, name="A default", engine_args={"tp": 2},
                             base_tps=50.0, notes="cookbook default", baseline=True),
        "B": await _seed_run(factory, sid=2, name="B ignored", engine_args={"tp": 2,
            "chunked_prefill_size": 4096},
                                    base_tps=55.0),
        "C": await _seed_run(factory, sid=3, name="C mem", engine_args={"tp": 2,
            "mem_fraction_static": 0.85},
                                    base_tps=60.0, notes="raise mem fraction", ),
        "D": await _seed_run(factory, sid=4, name="D kv fp8",
                                    engine_args={"tp": 2, "mem_fraction_static": 0.85,
                                        "kv_cache_dtype": "fp8_e4m3"},
                                    base_tps=75.0, notes="plus fp8 kv cache", gsm8k=0.95),
        "E": await _seed_run(factory, sid=5, name="E ignored", engine_args={"tp": 4},
                                    base_tps=90.0),
    }


# ------------------------------------------------------------------ run + parts


async def test_the_run_document_embeds_the_parts_unchanged(stack):
    http, factory = stack
    ids = await _seed_five(factory)
    a = ids["A"]
    doc = (await http.get(f"/api/agent/v1/runs/{a}")).json()
    assert doc["kind"] == "run" and doc["schema_version"] == 1
    for part in ("launch", "environment", "benchmark", "results"):
        alone = (await http.get(f"/api/agent/v1/runs/{a}/{part}")).json()
        assert alone == doc[part], part
    assert doc["links"]["raw"].endswith(f"/runs/{a}/results/raw")


async def test_launch_is_structured_and_rendered_side_by_side(stack):
    http, factory = stack
    ids = await _seed_five(factory)
    launch = (await http.get(f"/api/agent/v1/runs/{ids['D']}/launch")).json()
    cfg = launch["config"]
    assert cfg["engine"] == "sglang" and cfg["image"] == IMAGE
    assert cfg["engine_args"] == {"tp": 2, "mem_fraction_static": 0.85,
        "kv_cache_dtype": "fp8_e4m3"}
    assert cfg["gpu_type"] == "H100"
    assert launch["cards"] == 2
    rendered = launch["rendered"]
    assert rendered["docker_command"].startswith("docker run -d")
    assert "--tp-size 2" in rendered["engine_command"]
    assert "--kv-cache-dtype fp8_e4m3" in rendered["engine_command"]
    assert rendered["engine_flags"] == {
        "kv_cache_dtype": "--kv-cache-dtype", "mem_fraction_static": "--mem-fraction-static",
        "tp": "--tp-size",
    }
    origin = launch["origin"]
    assert origin["source"] == "campaign" and origin["campaign_id"] == 1
    assert origin["campaign_name"] == "GLM · H100"
    assert origin["candidate_id"] == ids["D"]


async def test_environment_lifts_what_the_probe_recorded_and_nothing_else(stack):
    http, factory = stack
    ids = await _seed_five(factory)
    env = (await http.get(f"/api/agent/v1/runs/{ids['A']}/environment")).json()
    assert env["machine"] == {"name": "node-5", "gpu_type": "H100", "gpu_count": 8,
        "driver": "ssh_docker"}
    assert env["engine_version"] == "0.5.15" and env["cuda_version"] == "12.9"
    assert env["image_digest"] == "sha256:feed" and env["gpu_indices"] == [0, 1]
    assert env["snapshot"]["gpu_name"] == "NVIDIA H100"


async def test_benchmark_keeps_frozen_params_and_the_platform_overlay_apart(stack):
    http, factory = stack
    ids = await _seed_five(factory)
    bench = (await http.get(f"/api/agent/v1/runs/{ids['A']}/benchmark")).json()
    assert bench["llmbench"]["slug"] == SLUG
    assert bench["llmbench"]["config_hash_frozen"] == "abc12345"
    sweep, quality = bench["modules"]
    assert sweep["key"] == SWEEP and sweep["is_scenario"] and sweep["label"] == "8k + 1k"
    assert sweep["params"]["concurrencies"] == "1,2,4,8"
    assert {c["key"]: c["role"] for c in sweep["metric_configs"]} == {
        "uptime": "redline", "ttft_p99_ms": "redline", "output_tps": "score",
    }
    assert quality["key"] == "opencompass" and not quality["is_scenario"]
    platform = bench["platform"]
    assert platform["ranking_metric"] == f"{SWEEP}.output_tpm_card_norm"
    # the gate is read off the campaign's own redlines, not a second declaration
    assert platform["gate_text"] == "TTFT p50 ≤ 60000 ms, ≥ 15 tok/s per request"
    assert platform["objective"] == OBJECTIVE


async def test_results_come_at_four_resolutions(stack):
    http, factory = stack
    ids = await _seed_five(factory)
    res = (await http.get(f"/api/agent/v1/runs/{ids['A']}/results")).json()
    assert res["status"] == "measured" and res["passed"] is True
    head = res["headline"]
    assert head["ranking_metric"] == f"{SWEEP}.output_tpm_card_norm"
    assert head["ranking_value"] == res["metrics"][f"{SWEEP}.output_tpm_card_norm"]
    assert head["peak_concurrency"] == 8 and head["meets_gate"] is True
    assert head["quality"] == {"opencompass.gsm8k": 0.93}
    (scenario,) = res["scenarios"]
    assert scenario["label"] == "8k + 1k" and scenario["input_tokens"] == 8000
    assert scenario["summary"]["concurrency"] == 8 and scenario["summary"]["meets_gate"] is True
    assert [lv["concurrency"] for lv in scenario["levels"]] == [1, 2, 4, 8]
    assert scenario["levels"][0]["metrics"]["ttft_p50_ms"] == 340
    assert scenario["metrics"]["reported_concurrency"] == 8
    assert res["quality"][0]["scores"] == {"gsm8k": 0.93}
    roles = {(v["module"], v["metric"]): v for v in res["verdicts"]}
    assert roles[(SWEEP, "uptime")]["ok"] is True and roles[(SWEEP, "uptime")]["min"] == 0.99
    assert roles[("opencompass", "gsm8k")]["role"] == "quality_floor"
    assert roles[("opencompass", "gsm8k")]["ok"] is True
    assert res["failure"] is None
    raw = (await http.get(f"/api/agent/v1/runs/{ids['A']}/results/raw")).json()
    assert raw["runs"][0]["metrics_json"]["c1"]["output_tps"] == 45.0


async def test_a_failed_run_answers_with_a_failure_block_and_a_running_one_says_wait(stack):
    http, factory = stack
    await _seed_five(factory)
    failed = await _seed_run(factory, sid=6, name="F crash", engine_args={"tp": 1},
                                    base_tps=0, failed=True)
    res = (await http.get(f"/api/agent/v1/runs/{failed}/results")).json()
    assert res["status"] == "failed" and res["scenarios"] == []
    assert res["failure"]["failure_class"] == "engine_crash"
    assert res["failure"]["error"] == "CUDA out of memory"
    assert res["failure"]["log"].endswith(f"/runs/{failed}/log")

    running = await _seed_run(factory, sid=7, name="G running", engine_args={"tp": 1},
                                     base_tps=0, running=True)
    r = await http.get(f"/api/agent/v1/runs/{running}/results")
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "run_not_finished"
    assert r.json()["detail"]["wait"] is True

    assert (await http.get("/api/agent/v1/runs/999")).status_code == 404


# ------------------------------------------------------------------ campaigns


async def test_the_campaign_document_names_every_run_a_comparison_can_pick(stack):
    http, factory = stack
    ids = await _seed_five(factory)
    listed = (await http.get("/api/agent/v1/campaigns")).json()
    assert [c["id"] for c in listed] == [1]
    assert listed[0]["run_count"] == 5
    assert listed[0]["ranking_metric"] == f"{SWEEP}.output_tpm_card_norm"

    doc = (await http.get("/api/agent/v1/campaigns/1")).json()
    assert doc["kind"] == "campaign" and doc["baseline_run_id"] == ids["A"]
    rows = {r["run_id"]: r for r in doc["runs"]}
    assert set(rows) == set(ids.values())
    assert rows[ids["C"]]["engine_args"]["mem_fraction_static"] == 0.85
    assert rows[ids["A"]]["is_baseline"] is True
    assert rows[ids["E"]]["cards"] == 4
    assert rows[ids["D"]]["headline"]["ranking_value"] > rows[ids["A"]]["headline"][
        "ranking_value"
    ]
    # The campaign's benchmark is the frozen document, with no run attached.
    assert doc["benchmark"]["run_id"] is None
    assert doc["benchmark"]["modules"][0]["params"]["input_tokens"] == 8000


# ------------------------------------------------------------------ comparison


async def test_the_comparison_holds_only_the_chosen_runs(stack):
    http, factory = stack
    ids = await _seed_five(factory)
    r = await http.get(
        "/api/agent/v1/comparison", params={"baseline": ids["A"],
            "attempts": f"{ids['C']},{ids['D']}"}
    )
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["comparable"] is True and doc["reasons"] == []
    assert doc["baseline"]["run_id"] == ids["A"]
    assert doc["baseline"]["label"] == f"GLM · H100 · run {ids['A']}"
    assert [a["run_id"] for a in doc["attempts"]] == [ids["C"], ids["D"]]
    assert [a["position"] for a in doc["attempts"]] == [1, 2]
    text = r.text
    assert "B ignored" not in text and "E ignored" not in text
    assert str(ids["B"]) not in [str(x["run_id"]) for x in doc["attempts"]]
    for line in (line for s in doc["series"] for line in s["lines"]):
        assert line["run_id"] in (ids["A"], ids["C"], ids["D"])


async def test_deltas_and_diffs_are_computed_by_the_platform(stack):
    http, factory = stack
    ids = await _seed_five(factory)
    doc = (await http.get(
        "/api/agent/v1/comparison", params={"baseline": ids["A"],
            "attempts": f"{ids['C']},{ids['D']}"}
    )).json()
    c, d = doc["attempts"]
    # C adds one knob to the baseline.
    assert c["diff_vs_baseline"]["engine_args"] == {
        "added": {"mem_fraction_static": 0.85}, "removed": {}, "changed": {},
    }
    assert c["diff_vs_baseline"]["changed"] is True and c["diff_vs_baseline"]["cards"] is None
    # D adds the kv dtype on top of C: the ablation step is one knob.
    assert d["diff_vs_previous"]["engine_args"]["added"] == {"kv_cache_dtype": "fp8_e4m3"}
    assert set(d["diff_vs_baseline"]["engine_args"]["added"]) == {"mem_fraction_static",
        "kv_cache_dtype"}
    # Signed percentages with a direction: throughput up is good, TTFT the same is neutral.
    rank = d["deltas"]["headline"]["ranking_value"]
    assert rank["baseline"] < rank["attempt"] and rank["pct"] == 50.0 and rank["improved"] is True
    assert rank["better"] == "higher"
    (scenario,) = d["deltas"]["scenarios"]
    assert scenario["key"] == SWEEP
    c1 = next(lv for lv in scenario["levels"] if lv["concurrency"] == 1)["metrics"]
    assert c1["output_tps"]["pct"] == 50.0
    assert c1["ttft_p50_ms"]["better"] == "lower" and c1["ttft_p50_ms"]["improved"] is None
    assert d["deltas"]["quality"]["opencompass.gsm8k"]["pct"] == 2.15
    prev = d["deltas_vs_previous"]["headline"]["ranking_value"]
    assert prev["pct"] == 25.0
    assert doc["best"]["overall"]["run_id"] == ids["D"]
    assert doc["best"]["per_scenario"][0]["run_id"] == ids["D"]
    # Chart-ready: one series per (scenario, metric), one line per run.
    tps = next(s for s in doc["series"] if s["metric"] == "output_tps")
    assert tps["scenario"] == SWEEP and tps["better"] == "higher" and tps["unit"] == "tok/s"
    assert [line["label"] for line in tps["lines"]] == [
        f"GLM · H100 · run {ids[k]}" for k in ("A", "C", "D")
    ]
    assert tps["lines"][0]["points"][0] == [1.0, 45.0]
    ttft = next(s for s in doc["series"] if s["metric"] == "ttft_p50_ms")
    assert ttft["better"] == "lower" and ttft["unit"] == "ms"
    assert doc["catalog"]["output_tpm_card_norm"]["better"] == "higher"
    assert doc["catalog"]["ttft_p50_ms"]["unit"] == "ms"
    assert doc["rendered"] is None


async def test_markdown_rendering(stack):
    http, factory = stack
    ids = await _seed_five(factory)
    r = await http.get(
        "/api/agent/v1/comparison",
        params={"baseline": ids["A"], "attempts": f"{ids['C']},{ids['D']}",
                "format": "markdown"},
    )
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["baseline"]["run_id"] == ids["A"]
    rendered = doc["rendered"]
    assert rendered["headline"].startswith("| Config | Engine | Cards |")
    assert "(baseline)" in rendered["headline"] and "+50.0% ✅" in rendered["headline"]
    assert SWEEP in rendered["scenarios"]
    assert "output_tps" in rendered["levels"][SWEEP]
    assert "| 1 | 45.0 | 54.0 | 67.5 |" in rendered["levels"][SWEEP]["output_tps"]
    assert "--mem-fraction-static" in rendered["diffs"][str(ids["C"])]
    # what a person runs, not how the platform ran it
    launch = rendered["launch"][str(ids["A"])]
    assert launch.startswith("```bash\nsglang serve \\\n  --model-path /mnt/models/GLM-5.2")
    assert "--tp-size 2" in launch and "docker" not in launch
    summary = rendered["summary"]
    assert summary.startswith(
        f"|  | GLM · H100 · run {ids['A']} | GLM · H100 · run {ids['C']} "
        f"| GLM · H100 · run {ids['D']} |"
    )
    assert "| 8k + 1k throughput |" in summary and "tok/s/GPU @ c8" in summary
    assert "(+50.0%)" in summary and "| quality: gsm8k |" in summary
    assert "| ranked by: " in summary

    # the best level is the highest throughput that met the SLO, per GPU (2 cards)
    best = doc["baseline"]["results"]["scenarios"][0]["best_level"]
    assert best["concurrency"] == 4 and best["total_tps_per_gpu"] == 100.0
    assert best["output_tps_per_gpu"] == 90.0 and best["input_tps_per_gpu"] == 10.0
    d_best = next(a for a in doc["attempts"] if a["run_id"] == ids["D"])
    assert d_best["deltas"]["scenarios"][0]["best_level"]["total_tps_per_gpu"]["pct"] == 50.0


async def test_failed_attempts_stay_in_the_comparison(stack):
    http, factory = stack
    ids = await _seed_five(factory)
    failed = await _seed_run(factory, sid=6, name="F crash", engine_args={"tp": 1},
                                    base_tps=0, failed=True)
    doc = (await http.get(
        "/api/agent/v1/comparison", params={"baseline": ids["A"],
            "attempts": f"{ids['C']},{failed}"}
    )).json()
    assert doc["comparable"] is True
    f = doc["attempts"][1]
    assert f["results"]["status"] == "failed"
    assert f["results"]["failure"]["failure_class"] == "engine_crash"
    assert f["deltas"]["headline"]["ranking_value"]["attempt"] is None
    assert f["diff_vs_baseline"]["engine_args"]["changed"] == {"tp": {"from": 2, "to": 1}}
    assert f["diff_vs_baseline"]["cards"] == {"from": 2, "to": 1}


async def test_a_quality_redline_that_flips_is_a_verdict_change_even_with_a_floor_on_it(stack):
    http, factory = stack
    ids = await _seed_five(factory)
    # gsm8k 0.5 is under the benchmark's own 0.6 redline AND the objective's 0.9 floor.
    worse = await _seed_run(factory, sid=6, name="F worse quality", engine_args={"tp": 2},
                                   base_tps=80.0, gsm8k=0.5)
    doc = (await http.get(
        "/api/agent/v1/comparison", params={"baseline": ids["A"], "attempts": str(worse)}
    )).json()
    (att,) = doc["attempts"]
    changes = {(c["metric"], c["role"]): (c["baseline_ok"],
        c["attempt_ok"]) for c in att["verdict_changes"]}
    assert changes[("gsm8k", "redline")] == (True, False)
    assert changes[("gsm8k", "quality_floor")] == (True, False)
    # Faster, but it no longer passes: best overall stays with a passing run.
    assert att["deltas"]["headline"]["ranking_value"]["improved"] is True
    assert doc["best"]["overall"]["run_id"] == ids["A"]


async def test_runs_that_measured_different_things_are_refused_unless_forced(stack):
    http, factory = stack
    ids = await _seed_five(factory)
    other = await _seed_run(factory, sid=8, name="H long ctx", engine_args={"tp": 2},
                                   base_tps=30.0, params={"input_tokens": 32000,
                                       "output_tokens": 100})
    r = await http.get("/api/agent/v1/comparison", params={"baseline": ids["A"],
        "attempts": str(other)})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["error"] == "not_comparable"
    codes = {(x["code"], x["field"]) for x in detail["reasons"]}
    assert ("module_params_differ", "input_tokens") in codes
    assert ("module_params_differ", "output_tokens") in codes

    forced = await http.get(
        "/api/agent/v1/comparison", params={"baseline": ids["A"], "attempts": str(other),
            "force": "true"}
    )
    assert forced.status_code == 200
    assert forced.json()["comparable"] is False and len(forced.json()["reasons"]) == 2

    running = await _seed_run(factory, sid=9, name="I running", engine_args={"tp": 2},
                                     base_tps=0, running=True)
    r = await http.get("/api/agent/v1/comparison", params={"baseline": ids["A"],
        "attempts": str(running)})
    assert r.status_code == 422
    assert r.json()["detail"]["reasons"][0]["code"] == "run_not_finished"

    same = await http.get("/api/agent/v1/comparison", params={"baseline": ids["A"],
        "attempts": str(ids["A"])})
    assert same.status_code == 422 and same.json()["detail"]["error"] == "baseline_is_an_attempt"


async def test_a_replay_is_a_scenario_with_metrics_instead_of_levels(stack):
    http, factory = stack
    base = await _seed_run(factory, sid=11, name="R base", engine_args={"tp": 2},
                                  base_tps=50.0, replay_concurrency=8)
    tuned = await _seed_run(factory, sid=12, name="R fp8",
                                   engine_args={"tp": 2, "quantization": "fp8"},
                                   base_tps=60.0, replay_concurrency=8)
    doc = (await http.get(f"/api/agent/v1/runs/{base}")).json()
    replay = next(x for x in doc["results"]["scenarios"] if x["kind"] == "replay")
    assert replay["label"] == "replay" and replay["levels"] == []
    assert replay["input_tokens"] is None
    assert replay["summary"]["concurrency"] == 8
    assert replay["summary"]["request_output_tps"] == 25.0
    assert replay["metrics"]["score_card_norm"] == 200000.0
    # derived at harvest, so only in the flat metrics — still part of the scenario
    async with factory() as db:
        res = (await db.execute(select(Result).where(Result.run_id == base))).scalar_one()
        res.metrics = {**res.metrics, "replay.derived_card_norm": 7.0}
        await db.commit()
    doc = (await http.get(f"/api/agent/v1/runs/{base}")).json()
    replay = next(x for x in doc["results"]["scenarios"] if x["kind"] == "replay")
    assert replay["metrics"]["derived_card_norm"] == 7.0
    # a throughput scenario, not a quality suite: its floors are not quality floors
    assert [q["key"] for q in doc["results"]["quality"]] == ["opencompass"]
    module = next(m for m in doc["benchmark"]["modules"] if m["key"] == "replay")
    assert module["is_scenario"] is True and module["label"] == "replay"

    r = await http.get("/api/agent/v1/comparison", params={
        "baseline": base, "attempts": str(tuned), "format": "markdown"})
    assert r.status_code == 200, r.text
    cmp_doc = r.json()
    deltas = next(d for d in cmp_doc["attempts"][0]["deltas"]["scenarios"]
                  if d["key"] == "replay")
    assert deltas["levels"] == []
    assert deltas["best_level"]["total_tps_per_gpu"]["pct"] == 20.0
    assert deltas["metrics"]["score_card_norm"]["pct"] == 20.0
    assert deltas["metrics"]["score_card_norm"]["improved"] is True
    assert deltas["metrics"]["ttft_16k_32k_count"]["improved"] is None  # a count moves, neutrally
    assert "replay" not in {s["scenario"] for s in cmp_doc["series"]}
    table = cmp_doc["rendered"]["replay"]["replay"]
    assert "| score_card_norm |" in table and "+20.0% ✅" in table
    assert "ttft_ge_256k" not in table  # nobody landed in that bucket

    harder = await _seed_run(factory, sid=13, name="R c16", engine_args={"tp": 2},
                                    base_tps=60.0, replay_concurrency=16)
    r = await http.get("/api/agent/v1/comparison", params={
        "baseline": base, "attempts": str(harder)})
    assert r.status_code == 422
    reasons = r.json()["detail"]["reasons"]
    assert {(x["module"], x["field"]) for x in reasons} == {("replay", "concurrency")}


# ------------------------------------------------------------------ reports


async def test_a_saved_report_freezes_the_comparison_and_serves_its_assets(stack):
    http, factory = stack
    ids = await _seed_five(factory)
    png = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()
    r = await http.post("/api/agent/v1/reports", json={
        "title": "Optimizing GLM-5.2 on H100",
        "baseline_run_id": ids["A"], "attempt_run_ids": [ids["C"], ids["D"]],
        "markdown": "# Report\n\n![tps](output_tps.png)\n",
        "assets": [{"name": "output_tps.png", "content_type": "image/png", "data_base64": png}],
        "generator": {"agent": "claude-code", "model": "claude-opus-5"},
    })
    assert r.status_code == 201, r.text
    saved = r.json()
    assert saved["url"] == f"/reports/{saved['id']}" and saved["campaign_id"] == 1
    assert saved["comparable"] is True and saved["asset_names"] == ["output_tps.png"]
    assert saved["comparison"]["baseline"]["run_id"] == ids["A"]
    assert [a["run_id"] for a in saved["comparison"]["attempts"]] == [ids["C"], ids["D"]]
    assert saved["created_by_name"] == "admin"

    listed = (await http.get("/api/agent/v1/reports", params={"campaign_id": 1})).json()
    assert [x["id"] for x in listed] == [saved["id"]]
    detail = (await http.get(f"/api/agent/v1/reports/{saved['id']}")).json()
    assert detail["markdown"].startswith("# Report")
    asset = await http.get(f"/api/agent/v1/reports/{saved['id']}/assets/output_tps.png")
    assert asset.status_code == 200 and asset.headers["content-type"] == "image/png"
    assert asset.content.startswith(b"\x89PNG")
    missing = await http.get(f"/api/agent/v1/reports/{saved['id']}/assets/nope.png")
    assert missing.status_code == 404


async def test_a_report_over_incomparable_runs_must_say_so(stack):
    http, factory = stack
    ids = await _seed_five(factory)
    other = await _seed_run(factory, sid=8, name="H long ctx", engine_args={"tp": 2},
                                   base_tps=30.0, params={"input_tokens": 32000})
    body = {
        "title": "t", "baseline_run_id": ids["A"], "attempt_run_ids": [other], "markdown": "x",
    }
    refused = await http.post("/api/agent/v1/reports", json=body)
    assert refused.status_code == 422 and refused.json()["detail"]["error"] == "not_comparable"
    allowed = await http.post("/api/agent/v1/reports", json={**body, "comparable": False})
    assert allowed.status_code == 201 and allowed.json()["comparable"] is False

    bad = await http.post("/api/agent/v1/reports", json={
        **body, "attempt_run_ids": [ids["C"]],
        "assets": [{"name": "x.png", "data_base64": "not base64!"}],
    })
    assert bad.status_code == 422 and bad.json()["detail"]["error"] == "bad_asset"


BLOCKED_REPORT = """# Optimizing GLM-5.2 on H100

```chart
type: summary
```

```table
type: summary
```

```chart
type: sweep
scenario: perf_guidellm_sweep
```

```table
type: diff
attempt: 2
```

```command
config: 2
```

```bash
echo a plain code block is not a block
```
"""


async def test_report_blocks_are_listed_checked_and_saved(stack):
    http, factory = stack
    ids = await _seed_five(factory)
    listed = await http.get("/api/agent/v1/report-blocks", params={
        "baseline": ids["A"], "attempts": f"{ids['C']},{ids['D']}"})
    assert listed.status_code == 200, listed.text
    snippets = {b["markdown"] for b in listed.json()}
    assert "```chart\ntype: summary\n```" in snippets
    assert "```chart\ntype: sweep\nscenario: perf_guidellm_sweep\n```" in snippets
    assert "```table\ntype: diff\nattempt: 2\n```" in snippets
    assert "```command\nconfig: baseline\n```" in snippets
    assert not any("agentic" in x for x in snippets)  # no replay in this benchmark

    body = {"title": "t", "baseline_run_id": ids["A"],
            "attempt_run_ids": [ids["C"], ids["D"]], "markdown": BLOCKED_REPORT}
    dry = await http.post("/api/agent/v1/reports", params={"dry_run": "true"}, json=body)
    assert dry.status_code == 200 and dry.json() == {"ok": True, "blocks": 5, "comparable": True}
    assert (await http.get("/api/agent/v1/reports")).json() == []  # a dry run saves nothing

    wrong = BLOCKED_REPORT.replace("scenario: perf_guidellm_sweep", "scenario: nope") \
        .replace("attempt: 2", "attempt: 7").replace("type: summary\n```\n\n```table",
                                                    "type: pie\n```\n\n```table")
    bad = await http.post("/api/agent/v1/reports", json={**body, "markdown": wrong})
    assert bad.status_code == 422
    detail = bad.json()["detail"]
    assert detail["error"] == "bad_blocks"
    problems = [x["problem"] for x in detail["reasons"]]
    assert any("chart type must be one of" in x for x in problems)
    assert any("no scenario 'nope'" in x for x in problems)
    assert any("no attempt '7'" in x for x in problems)

    labels = await http.post("/api/agent/v1/reports", json={**body, "labels": ["only one"]})
    assert labels.status_code == 422 and labels.json()["detail"]["error"] == "bad_labels"

    en = await http.post("/api/agent/v1/reports", json={
        **body, "labels": ["Baseline", "mem", "mem + fp8 kv"]})
    assert en.status_code == 201, en.text
    en = en.json()
    assert en["lang"] == "en" and en["labels"] == ["Baseline", "mem", "mem + fp8 kv"]
    zh = await http.post("/api/agent/v1/reports", json={
        **body, "title": "在 H100 上优化 GLM-5.2", "lang": "zh", "translation_of": en["id"]})
    assert zh.status_code == 201, zh.text
    zh = zh.json()
    assert zh["translation_of"] == en["id"]
    assert [(x["id"], x["lang"]) for x in zh["translations"]] == [
        (en["id"], "en"), (zh["id"], "zh")]
    again = await http.post("/api/agent/v1/reports", json={
        **body, "lang": "zh", "translation_of": zh["id"]})
    assert again.status_code == 409 and again.json()["detail"]["error"] == "language_exists"
    other_runs = await http.post("/api/agent/v1/reports", json={
        **body, "attempt_run_ids": [ids["C"]], "lang": "zh", "translation_of": en["id"]})
    assert other_runs.status_code == 422

    detail = (await http.get(f"/api/agent/v1/reports/{en['id']}")).json()
    assert [x["lang"] for x in detail["translations"]] == ["en", "zh"]


async def test_a_report_exports_as_one_self_contained_html_file(stack, tmp_path, monkeypatch):
    from app.agent import export

    http, factory = stack
    ids = await _seed_five(factory)
    body = {"title": "Optimizing GLM-5.2 on H100", "baseline_run_id": ids["A"],
            "attempt_run_ids": [ids["C"], ids["D"]],
            "markdown": BLOCKED_REPORT + "\n</script><script>alert(1)</script>\n",
            "assets": [{"name": "a.png", "data_base64": base64.b64encode(b"png").decode()}]}
    en = (await http.post("/api/agent/v1/reports", json=body)).json()
    zh = (await http.post("/api/agent/v1/reports", json={
        **body, "title": "中文标题", "lang": "zh", "translation_of": en["id"]})).json()

    monkeypatch.setattr(export, "RENDERER", tmp_path / "missing.js")
    r = await http.get(f"/api/agent/v1/reports/{en['id']}/export.html")
    assert r.status_code == 503 and r.json()["detail"]["error"] == "renderer_not_built"

    bundle = tmp_path / "report-renderer.js"
    bundle.write_text("var AutotuneReport={mountStandalone:function(){}};", encoding="utf-8")
    monkeypatch.setattr(export, "RENDERER", bundle)
    r = await http.get(f"/api/agent/v1/reports/{zh['id']}/export.html", params={"download": "true"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "attachment" in r.headers["content-disposition"]
    html = r.text
    assert '<html lang="zh-CN">' in html and "<title>中文标题</title>" in html
    assert "var AutotuneReport=" in html  # the renderer rides inside
    assert "data:image/png;base64," in html  # assets inlined
    # the agent's text cannot close the data element and run script of its own
    assert "</script><script>alert(1)" not in html
    data = html.split('id="autotune-report-data">', 1)[1].split("</script>", 1)[0]
    payload = json.loads(data.replace("<\\/", "</"))
    assert payload["initial"] == "zh"
    assert [x["lang"] for x in payload["reports"]] == ["en", "zh"]
    assert payload["reports"][0]["comparison"]["baseline"]["run_id"] == ids["A"]


async def test_a_report_downloads_as_a_zip_of_its_sources(stack, tmp_path, monkeypatch):
    import io
    import zipfile

    from app.agent import export

    http, factory = stack
    ids = await _seed_five(factory)
    body = {"title": "Optimizing GLM-5.2 on H100", "baseline_run_id": ids["A"],
            "attempt_run_ids": [ids["C"], ids["D"]], "markdown": BLOCKED_REPORT,
            "labels": ["Prod", "Tuned", "Tuned 2"],
            "assets": [{"name": "a.png", "data_base64": base64.b64encode(b"png").decode()}]}
    en = (await http.post("/api/agent/v1/reports", json=body)).json()
    zh = (await http.post("/api/agent/v1/reports", json={
        **body, "title": "中文标题", "lang": "zh", "translation_of": en["id"]})).json()
    bundle = tmp_path / "report-renderer.js"
    bundle.write_text("var AutotuneReport={mountStandalone:function(){}};", encoding="utf-8")
    monkeypatch.setattr(export, "RENDERER", bundle)

    r = await http.get(f"/api/agent/v1/reports/{zh['id']}/bundle.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(r.content))
    root = z.namelist()[0].split("/")[0]
    names = {n.split("/", 1)[1] for n in z.namelist()}
    assert names == {"README.md", "report.json", "report.en.md", "report.zh.md",
                     "comparison.json", "a.png", "report.html", "report-renderer.js"}
    manifest = json.loads(z.read(f"{root}/report.json"))
    assert manifest["initial"] == "zh"
    assert [(x["lang"], x["markdown"], x["comparison"]) for x in manifest["reports"]] == [
        ("en", "report.en.md", "comparison.json"), ("zh", "report.zh.md", "comparison.json")]
    assert manifest["reports"][0]["labels"] == ["Prod", "Tuned", "Tuned 2"]
    assert z.read(f"{root}/report.en.md").decode() == BLOCKED_REPORT
    assert json.loads(z.read(f"{root}/comparison.json"))["baseline"]["run_id"] == ids["A"]
    assert z.read(f"{root}/a.png") == b"png"

    # without a built renderer the sources still download
    monkeypatch.setattr(export, "RENDERER", tmp_path / "missing.js")
    z = zipfile.ZipFile(io.BytesIO(
        (await http.get(f"/api/agent/v1/reports/{en['id']}/bundle.zip")).content))
    assert not any(n.endswith(("report.html", "report-renderer.js")) for n in z.namelist())
    assert (await http.get("/api/agent/v1/reports/9999/bundle.zip")).status_code == 404


def test_the_committed_renderer_bundle_is_there():
    """The export inlines frontend/src/report built by `npm run build:report`."""
    from app.agent import export

    assert export.RENDERER.is_file(), "run `npm run build:report` in frontend/ and commit it"
    assert "mountStandalone" in export.RENDERER.read_text(encoding="utf-8")


async def test_the_entry_point_lists_where_everything_is(stack):
    http, _ = stack
    index = (await http.get("/api/agent/v1/")).json()
    assert index["kind"] == "agent_api"
    assert index["links"]["campaigns"] == "/api/agent/v1/campaigns"


def test_the_fixture_payload_is_what_the_harvest_would_freeze():
    raw = _raw(50.0)
    reports = module_reports(copy.deepcopy(raw))
    assert [r["module"] for r in reports] == [SWEEP, "opencompass"]
    assert reports[0]["params"]["input_tokens"] == 8000


