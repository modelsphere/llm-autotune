"""A campaign describes its workload; AutoTune makes the benchmark.

The person creating a campaign says "random prompts, 2048 in, 512 out, at
these concurrencies" or "replay this traffic", and AutoTune creates the
matching benchmark on LLMBench, filed under its own group and locked. The same
workload always becomes the same benchmark, so asking again reuses it.
"""

from itertools import count

import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import campaigns as campaigns_api
from app.core.auth import create_token
from app.db.base import Base, get_async_session
from app.db.models import Campaign, User
from app.evaluation import benchmark_spec as bs
from app.evaluation.benchmarks import GROUP_TAG, Ensured, ensure_benchmark
from app.evaluation.llmbench import LLMBenchClient
from app.main import app
from app.mocks import llmbench_mock


@pytest.fixture
def mock_llmbench():
    before = set(llmbench_mock.BENCHMARKS)
    with TestClient(llmbench_mock.app) as http:
        yield LLMBenchClient(base_url="http://testserver", api_key="llmb_test",
                             transport=http._transport, max_attempts=1)
    for slug in set(llmbench_mock.BENCHMARKS) - before:
        llmbench_mock.BENCHMARKS.pop(slug, None)
        llmbench_mock._locked.discard(slug)
    llmbench_mock._group_tags.clear()


# -- the spec --------------------------------------------------------------------


def test_the_same_workload_is_the_same_benchmark():
    a = bs.BenchmarkSpec(kind="sweep", concurrencies=[64, 1, 16, 4])
    b = bs.BenchmarkSpec(kind="sweep", concurrencies=[1, 4, 16, 64], dataset_profile="ignored")
    assert a.slug == b.slug
    assert a.slug != bs.BenchmarkSpec(kind="sweep", input_tokens=4096).slug
    assert a.slug.startswith("autotune-sweep-")


def test_a_sweep_document_carries_the_workload_and_the_group():
    spec = bs.BenchmarkSpec(kind="sweep", input_tokens=1024, output_tokens=128,
                            concurrencies=[2, 8])
    doc = bs.document(spec)
    params = doc["modules"][0]["params"]
    assert (params["input_tokens"], params["output_tokens"], params["concurrencies"]) == (
        1024, 128, "2,8")
    # Each level is a sample of requests per slot; the duration only caps it.
    assert (params["requests_per_concurrency"], params["max_seconds"]) == (20, 600)
    assert doc["group_tags"] == [f"{GROUP_TAG}/sweep"]
    assert doc["status"] == "active"


def test_a_replay_of_a_profile_resolves_it_and_the_example_set_does_not():
    rolling = bs.document(bs.BenchmarkSpec(kind="replay", dataset_profile="prod-sample"))
    params = rolling["modules"][0]["params"]
    assert (params["dataset_source"], params["dataset_profile"]) == ("auto", "prod-sample")
    example = bs.document(bs.BenchmarkSpec(kind="replay"))
    assert "dataset_source" not in example["modules"][0]["params"]
    assert example["modules"][0]["module_name"] == bs.replay_module()


def test_no_concurrency_level_is_refused():
    with pytest.raises(ValueError):
        bs.BenchmarkSpec(kind="sweep", concurrencies=[0])


# -- on LLMBench ---------------------------------------------------------------------


def test_a_workload_is_created_once_then_reused(mock_llmbench):
    spec = bs.BenchmarkSpec(kind="replay", dataset_profile=llmbench_mock.PROFILE["name"])
    first = bs.ensure_spec(spec, mock_llmbench)
    assert first.created and first.locked
    row = mock_llmbench.get_benchmark(spec.slug)
    assert row["group_tags"] == [f"{GROUP_TAG}/replay"]
    # The replay module's own evaluation, as LLMBench describes it.
    assert row["modules"][0]["metric_configs"] == llmbench_mock.REPLAY_CONFIGS
    again = bs.ensure_spec(spec, mock_llmbench)
    assert (again.slug, again.created, again.locked) == (spec.slug, False, True)


def test_an_untagged_benchmark_of_ours_is_filed_under_the_group(mock_llmbench):
    # The mock lists the screen benchmark as already created by this account,
    # from before AutoTune filed its benchmarks.
    ensure_benchmark(mock_llmbench)
    assert mock_llmbench.get_benchmark("autotune-screen-v1")["group_tags"] == [GROUP_TAG]


# -- through campaign creation -----------------------------------------------------------

_counter = count()


@pytest_asyncio.fixture
async def client(monkeypatch):
    made: list[bs.BenchmarkSpec] = []

    def _ensure(spec, client=None):
        made.append(spec)
        return Ensured(slug=spec.slug, benchmark_id=1, created=True, locked=True)

    monkeypatch.setattr(campaigns_api, "ensure_spec", _ensure)
    name = f"bench_spec_{next(_counter)}"
    engine = create_async_engine(f"sqlite+aiosqlite:///file:{name}?mode=memory&cache=shared&uri=true")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _session():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_async_session] = _session
    async with factory() as session:
        user = User(id=1, username="admin", password_hash="x", role="admin")
        session.add(user)
        await session.commit()
        token = create_token(user)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
        http.headers["Authorization"] = f"Bearer {token}"
        http.db = factory
        http.made = made
        yield http
    app.dependency_overrides.clear()
    await engine.dispose()


def _body(**extra):
    return {"name": "c", "engine": "sglang", "image": "img", "model_path": "/m",
            "search_space": {"grid": {"tp": [1]}}, **extra}


async def test_a_campaign_with_a_workload_gets_its_benchmark(client):
    sweep = {"kind": "sweep", "input_tokens": 1024}
    replay = {"kind": "replay", "dataset_profile": "prod-sample"}
    response = await client.post("/api/campaigns", json=_body(
        benchmark_spec=sweep, verify_benchmark_spec=replay, verify_top_k=2))
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["benchmark_slug"] == bs.BenchmarkSpec(**sweep).slug
    assert out["verify_benchmark_slug"] == bs.BenchmarkSpec(**replay).slug
    # A replay of a rolling profile pins that profile for the campaign's life.
    assert out["dataset_profile"] == "prod-sample"
    assert [s.kind for s in client.made] == ["sweep", "replay"]
    async with client.db() as session:
        assert (await session.execute(select(Campaign))).scalars().one()


async def test_a_malformed_workload_is_refused_before_anything_is_created(client):
    response = await client.post("/api/campaigns", json=_body(
        benchmark_spec={"kind": "sweep", "concurrencies": []}))
    assert response.status_code == 422
    assert client.made == []


async def test_the_spec_endpoint_names_the_benchmark_without_creating_it(client):
    response = await client.post("/api/benchmarks/spec", json={"kind": "sweep"})
    assert response.status_code == 200
    out = response.json()
    assert out["slug"] == bs.BenchmarkSpec().slug
    assert out["module"] == bs.SWEEP_MODULE
    assert client.made == []


def test_older_benchmarks_of_ours_are_filed_and_others_left_alone(mock_llmbench):
    from app.evaluation.benchmarks import file_untagged

    filed = file_untagged(mock_llmbench)
    assert "autotune-screen-v1" in filed
    for slug in filed:
        assert mock_llmbench.get_benchmark(slug)["group_tags"] == [GROUP_TAG]
    assert file_untagged(mock_llmbench) == [], "already filed: nothing to do twice"


# -- frozen datasets -----------------------------------------------------------------


def test_a_replay_of_a_frozen_dataset_replays_its_file(mock_llmbench):
    spec = bs.BenchmarkSpec(kind="replay", dataset_frozen="mock-frozen", requests=0)
    params = bs.document(spec, mock_llmbench)["modules"][0]["params"]
    assert (params["dataset_source"], params["dataset_path"]) == (
        "fixed", llmbench_mock.FROZEN["path"])
    assert "dataset_profile" not in params
    assert "mock-frozen" in spec.title
    # A different dataset is a different benchmark.
    assert spec.slug != bs.BenchmarkSpec(kind="replay", requests=0).slug

    with pytest.raises(bs.BenchmarkRefused, match="no frozen dataset named gone"):
        bs.document(bs.BenchmarkSpec(kind="replay", dataset_frozen="gone"), mock_llmbench)


def test_replays_written_before_frozen_datasets_keep_their_slug():
    import hashlib
    import json

    before = {"kind": "replay", "dataset_profile": "prod-sample", "requests": 500,
              "concurrency": 16}
    digest = hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest()[:8]
    assert bs.BenchmarkSpec(kind="replay", dataset_profile="prod-sample").slug == (
        f"autotune-replay-{digest}")


def test_a_replay_names_one_dataset():
    with pytest.raises(ValueError, match="not both"):
        bs.BenchmarkSpec(kind="replay", dataset_profile="a", dataset_frozen="b")


async def test_a_campaign_replaying_a_frozen_dataset_records_it_and_pins_nothing(
    client, monkeypatch
):
    replay = {"kind": "replay", "dataset_frozen": "prod-q36"}
    response = await client.post("/api/campaigns", json=_body(benchmark_spec=replay))
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["benchmark_slug"] == bs.BenchmarkSpec(**replay).slug
    assert (out["dataset_frozen"], out["dataset_profile"]) == ("prod-q36", "")

    from app.api import datasets as datasets_api

    class _Frozen:
        def frozen(self):
            return [{"name": "prod-q36", "records": 1000, "origin": "upload",
                     "path": "/x/prod-q36.jsonl.gz", "used_by": ["AutoTune replay"]},
                    {"name": "other", "records": 5}]

    monkeypatch.setattr(datasets_api, "DatasetProfileClient", _Frozen)
    listed = {d["name"]: d for d in (await client.get("/api/datasets/frozen")).json()["frozen"]}
    assert [c["name"] for c in listed["prod-q36"]["campaigns"]] == ["c"]
    assert listed["prod-q36"]["origin"] == "upload"
    assert listed["other"]["campaigns"] == []
    assert "path" not in listed["prod-q36"]  # LLMBench's own business


# -- several workloads in one benchmark --------------------------------------------


def _replay_then_sweep(**extra) -> bs.BenchmarkSpec:
    return bs.BenchmarkSpec(
        kind="replay", dataset_frozen="mock-frozen", requests=200, concurrency=32,
        more=[{"kind": "sweep", "concurrencies": [16, 64]}], **extra,
    )


def test_a_benchmark_can_run_several_workloads_in_order(mock_llmbench):
    spec = _replay_then_sweep(quality={"tasks": ["gpqa_diamond"], "questions": 50})
    doc = bs.document(spec, mock_llmbench)

    assert [m["module_name"] for m in doc["modules"]] == [
        bs.replay_module(), bs.SWEEP_MODULE, bs.QUALITY_MODULE]
    assert [m["order_index"] for m in doc["modules"]] == [0, 1, 2]
    # LLMBench's own score stays the first workload's.
    assert [m["weight"] for m in doc["modules"]] == [1.0, 0.0, 0.0]
    assert doc["modules"][0]["params"]["dataset_path"] == llmbench_mock.FROZEN["path"]
    assert doc["modules"][1]["params"]["concurrencies"] == "16,64"
    assert spec.modules == [bs.replay_module(), bs.SWEEP_MODULE, bs.QUALITY_MODULE]
    # Known by its first workload.
    assert spec.slug.startswith("autotune-replay-")
    assert doc["group_tags"] == [f"{GROUP_TAG}/replay"]
    assert "replay — mock-frozen" in spec.title and "; then sweep — " in spec.title
    assert "quality (gpqa_diamond" in spec.title


def test_a_module_run_twice_reports_under_its_own_key():
    spec = bs.BenchmarkSpec(
        kind="sweep", input_tokens=1024,
        more=[{"kind": "sweep", "input_tokens": 16384, "concurrencies": [4]}],
    )
    assert spec.modules == [bs.SWEEP_MODULE, f"{bs.SWEEP_MODULE}#2"]
    params = [m["params"]["input_tokens"] for m in bs.document(spec)["modules"]]
    assert params == [1024, 16384]


def test_further_workloads_are_part_of_what_the_benchmark_is():
    alone = bs.BenchmarkSpec(kind="sweep")
    a = {"kind": "sweep", "input_tokens": 8192}
    b = {"kind": "sweep", "input_tokens": 16384}
    assert bs.BenchmarkSpec(kind="sweep", more=[a]).slug != alone.slug
    assert bs.BenchmarkSpec(kind="sweep", more=[a, b]).slug != (
        bs.BenchmarkSpec(kind="sweep", more=[b, a]).slug), "the order is part of it"
    assert bs.BenchmarkSpec(kind="sweep", more=[a]).slug == (
        bs.BenchmarkSpec(kind="sweep", more=[{**a, "dataset_profile": "", "requests": 9}]).slug
    ), "a field the workload does not use changes nothing"


def test_a_benchmark_of_one_workload_keeps_the_slug_it_had():
    import hashlib
    import json

    before = {"kind": "sweep", "input_tokens": 2048, "output_tokens": 512,
              "concurrencies": [1, 4, 16, 64], "requests_per_concurrency": 20,
              "max_seconds_per_level": 600}
    digest = hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest()[:8]
    assert bs.BenchmarkSpec().slug == f"autotune-sweep-{digest}"
    assert bs.BenchmarkSpec().modules == [bs.SWEEP_MODULE]
    assert bs.BenchmarkSpec().title.startswith("AutoTune sweep — 2048 in / 512 out")


def test_a_further_replay_names_a_frozen_dataset():
    with pytest.raises(ValueError, match="frozen dataset"):
        bs.BenchmarkSpec(kind="sweep", more=[{"kind": "replay", "dataset_profile": "rolling"}])
    with pytest.raises(ValueError):
        bs.BenchmarkSpec(kind="sweep", more=[{"kind": "sweep"}] * 5)
    with pytest.raises(ValueError, match="not both"):
        bs.BenchmarkSpec(kind="sweep", more=[
            {"kind": "replay", "dataset_profile": "a", "dataset_frozen": "b"}])


def test_a_benchmark_of_several_workloads_is_created_and_locked(mock_llmbench):
    spec = _replay_then_sweep()
    ensured = bs.ensure_spec(spec, mock_llmbench)
    assert ensured.created and ensured.locked
    stored = mock_llmbench.get_benchmark(spec.slug)
    assert stored is not None
    assert not bs.ensure_spec(spec, mock_llmbench).created, "asked twice, made once"


async def test_the_spec_endpoint_names_every_module(client):
    response = await client.post("/api/benchmarks/spec", json={
        "kind": "sweep", "more": [{"kind": "sweep", "input_tokens": 8192}],
        "quality": {"tasks": ["gpqa_diamond"]},
    })
    assert response.status_code == 200, response.text
    assert response.json()["modules"] == [
        bs.SWEEP_MODULE, f"{bs.SWEEP_MODULE}#2", bs.QUALITY_MODULE]
    assert response.json()["module"] == bs.SWEEP_MODULE


def test_a_metric_is_checked_against_the_modules_a_benchmark_runs():
    spec = bs.BenchmarkSpec(kind="sweep", more=[{"kind": "sweep", "input_tokens": 8192}],
                            quality={"tasks": ["gpqa_diamond"]})
    assert bs.metric_problem(f"{bs.SWEEP_MODULE}.output_tps", spec) is None
    assert bs.metric_problem(f"{bs.SWEEP_MODULE}#2.output_tps", spec) is None
    assert bs.metric_problem("score_total", spec) is None
    assert "no module replay" in bs.metric_problem("replay.output_tpm", spec)
    assert "no module" in bs.metric_problem(f"{bs.SWEEP_MODULE}#3.output_tps", spec)
    assert bs.metric_problem("output_tpm_per_gpu", spec) is None
    assert bs.metric_problem("nonsense_measure/[x", spec) is not None


def test_fixed_output_lengths_is_its_own_benchmark_and_reaches_the_module():
    plain = bs.BenchmarkSpec(kind="replay", dataset_profile="p", requests=400)
    fixed = bs.BenchmarkSpec(
        kind="replay", dataset_profile="p", requests=400, fixed_output_lengths=True,
    )
    # Off by default, and off leaves the slug of every existing replay alone.
    assert "fixed_output_lengths" not in plain.essentials()
    assert fixed.essentials()["fixed_output_lengths"] is True
    assert plain.slug != fixed.slug
    assert "recorded lengths" in fixed.description
    assert bs.workload_module(fixed)["params"]["fixed_output_lengths"] is True
    assert "fixed_output_lengths" not in bs.workload_module(plain)["params"]


def test_a_timed_replay_takes_fixed_output_lengths_too():
    plain = bs.BenchmarkSpec(kind="replay", dataset_profile="p", schedule="timed")
    fixed = bs.BenchmarkSpec(kind="replay", dataset_profile="p", schedule="timed",
                             fixed_output_lengths=True)
    # Off leaves the slug of every existing timed replay alone.
    assert "fixed_output_lengths" not in plain.essentials()
    assert fixed.essentials()["fixed_output_lengths"] is True
    assert plain.slug != fixed.slug
    assert "recorded lengths" in fixed.description
    assert "recorded lengths" not in plain.description
    params = bs.workload_module(fixed)["params"]
    assert params["fixed_output_lengths"] is True and params["schedule"] == "timed"
    assert "fixed_output_lengths" not in bs.workload_module(plain)["params"]


def test_steady_state_only_is_its_own_benchmark_reaches_the_module_and_is_closed_replay_only():
    plain = bs.BenchmarkSpec(kind="replay", dataset_profile="p", requests=400)
    steady = bs.BenchmarkSpec(
        kind="replay", dataset_profile="p", requests=400, steady_state_only=True,
    )
    # Off by default, and off leaves the slug of every existing replay alone.
    assert "steady_state_only" not in plain.essentials()
    assert steady.essentials()["steady_state_only"] is True
    assert plain.slug != steady.slug
    assert "steady part" in steady.description
    assert bs.workload_module(steady)["params"]["steady_state_only"] is True
    assert "steady_state_only" not in bs.workload_module(plain)["params"]
    with pytest.raises(ValueError, match="closed replay"):
        bs.BenchmarkSpec(
            kind="replay", dataset_profile="p", schedule="timed", steady_state_only=True,
        )
    with pytest.raises(ValueError, match="closed replay"):
        bs.Workload(kind="sweep", steady_state_only=True)


async def test_bfcl_campaign_and_verification_default_to_total_tpm(client):
    response = await client.post("/api/campaigns", json=_body(
        benchmark_spec={"kind": "bfcl"}, verify_benchmark_spec={"kind": "bfcl"},
        verify_top_k=2))
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["objective"]["target_metric"] == "bfcl_v4.total_tpm"
    assert out["verify_objective"]["target_metric"] == "bfcl_v4.total_tpm"
    assert out["dataset_profile"] == ""
    for spec in client.made:
        assert "total_tpm" in bs.document(spec)["modules"][0]["params"]["required_metrics"]
    preview = await client.post("/api/benchmarks/spec", json={"kind": "bfcl"})
    assert preview.json()["slug"] == out["verify_benchmark_slug"] == out["benchmark_slug"]


async def test_bfcl_requires_tpm_only_for_the_stage_that_ranks_on_it(client):
    response = await client.post("/api/campaigns", json=_body(
        benchmark_spec={"kind": "bfcl"}, objective={"target_metric": "bfcl_v4.accuracy"},
        verify_benchmark_spec={"kind": "bfcl"}, verify_top_k=2))
    assert response.status_code == 200, response.text
    screen, verify = client.made
    assert "total_tpm" not in screen.bfcl.required_metrics
    assert verify.bfcl.required_metrics.count("total_tpm") == 1
    body = campaigns_api.PreflightRequest(**_body(
        benchmark_spec={"kind": "bfcl"}, objective={"target_metric": "bfcl_v4.accuracy"},
        verify_benchmark_spec={"kind": "bfcl"}))
    campaigns_api._with_specs(body, {})
    assert (body.benchmark_slug, body.verify_benchmark_slug) == (screen.slug, verify.slug)


def test_preflight_described_workload_includes_both_quality_modules():
    body = campaigns_api.PreflightRequest(
        engine="sglang", image="img", model_path="/m", search_space={"grid": {"tp": [1]}},
        benchmark_spec={"kind": "replay", "quality": {}, "bfcl_quality": {}},
    )
    catalog = {}
    campaigns_api._with_specs(body, catalog)
    assert catalog[body.benchmark_slug] == ["replay", "opencompass", "bfcl_v4"]
    from app.measures import problem_with

    assert problem_with("bfcl_v4/accuracy[overall]", catalog[body.benchmark_slug]) is None


def test_bfcl_template_is_created_once_and_native_defaults_are_discoverable(mock_llmbench):
    spec = bs.BenchmarkSpec(kind="bfcl")
    first = bs.ensure_spec(spec, mock_llmbench)
    assert first.created and first.locked
    assert not bs.ensure_spec(spec, mock_llmbench).created
    row = mock_llmbench.get_benchmark(spec.slug)
    assert row["modules"][0]["params_json"]["task_set"] == "bfcl-v4-tool-use-v1"
    assert mock_llmbench.module_defaults("bfcl_v4")["default_metric_configs"]
    assert ensure_benchmark(mock_llmbench, "autotune-bfcl-v4-tool-use-v1").locked


@pytest.mark.parametrize("metric", ["accuracy[overall]", "bfcl_v4/ttft_ms@p90", "bad/accuracy"])
async def test_manual_validation_refuses_invalid_quality_metric_references(client, metric):
    response = await client.post("/api/campaigns/1/validate", json={
        "configs": [{"tp": 2}], "machine": "node", "quality_metrics": [metric],
        "quality_max_drop": 5,
    })
    assert response.status_code == 422, response.text


def test_preflight_detects_an_llmbench_without_bfcl(mock_llmbench, monkeypatch):
    import httpx

    def unavailable(name):
        response = httpx.Response(404, request=httpx.Request("GET", f"http://bench/modules/{name}"))
        response.raise_for_status()

    monkeypatch.setattr(mock_llmbench, "module_defaults", unavailable)
    body = campaigns_api.PreflightRequest(**_body(benchmark_spec={"kind": "bfcl"}))
    checks = campaigns_api._benchmark_checks(body, {}, mock_llmbench)
    assert any(c.key == "bfcl_module" and c.status == "fail" for c in checks)


def test_a_grid_sweep_keeps_its_slug_and_its_module():
    """The fields auto mode added are only in the slug when set."""
    spec = bs.BenchmarkSpec(kind="sweep", concurrencies=[8, 32])
    assert "mode" not in spec.essentials() and "slo" not in spec.essentials()
    params = bs.workload_module(spec)["params"]
    assert params["search_mode"] == "grid" and params["concurrencies"] == "8,32"
    assert not any(k.startswith(("slo_", "search_max", "probe_")) for k in params)


def test_an_auto_sweep_searches_for_its_slo():
    spec = bs.BenchmarkSpec(kind="sweep", mode="auto", max_concurrency=256,
                         slo={"ttft_ms": 2000, "ttft_percentile": "p90",
                              "min_request_output_tps": 20})
    params = bs.workload_module(spec)["params"]
    assert params["search_mode"] == "auto" and "concurrencies" not in params
    assert params["search_max_concurrency"] == 256 and params["probe_requests_per_concurrency"] == 5
    assert params["search_granularity"] == 0 and params["max_total_seconds"] == 7200
    assert (params["slo_max_ttft_ms"], params["slo_ttft_percentile"],
            params["slo_min_request_output_tps"]) == (2000, "p90", 20)
    assert spec.slug != bs.BenchmarkSpec(kind="sweep").slug
    assert "concurrencies" not in spec.essentials(), "auto runs none of the grid's levels"
    assert "searching up to concurrency 256" in spec.title


def test_a_grid_sweep_may_report_a_fixed_level_or_its_peak():
    fixed = bs.BenchmarkSpec(kind="sweep", concurrencies=[8, 32], report_level="fixed",
                             fixed_level=32)
    assert bs.workload_module(fixed)["params"]["fixed_level"] == 32
    peak = bs.BenchmarkSpec(kind="sweep", report_level="peak")
    assert bs.workload_module(peak)["params"]["report_level"] == "peak"
    for bad in ({"report_level": "fixed", "fixed_level": 7, "concurrencies": [8]},
                {"mode": "auto", "report_level": "peak"},
                {"kind": "replay", "mode": "auto"},
                {"slo": {"ttft_ms": 1, "nope": 2}}):
        with pytest.raises(ValueError):
            bs.BenchmarkSpec(**{"kind": "sweep", **bad})
