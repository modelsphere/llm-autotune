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
