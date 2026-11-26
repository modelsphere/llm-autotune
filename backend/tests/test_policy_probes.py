"""A probe: a benchmark a policy describes itself (contract 1.8).

A policy may measure one of its engines with a workload of its own choosing
instead of the campaign's benchmark: a short sweep before the long one,
another concurrency, a quality suite. The platform creates the benchmark and
answers the result, and keeps it apart from the campaign's results, which
measured something else.
"""
# ruff: noqa: F811  (the `night` fixture is imported, then named as a parameter)

import pytest
from sqlalchemy import select

from app.api import policy_sessions
from app.db.models import Campaign, Event, PolicyTrial, Result, Run
from app.evaluation.base import EvalOutcome, EvalStatus, Evaluator
from app.evaluation.benchmark_spec import BenchmarkSpec
from app.evaluation.benchmarks import BenchmarkRefused, Ensured
from app.schemas.policy import PolicySettings
from tests.test_policy_sessions import (  # noqa: F401  (fixture)
    _boot,
    _serving_launch,
    night,
)

QUICK = {"kind": "sweep", "concurrencies": [4], "requests_per_concurrency": 5}
LONG = {"kind": "sweep", "concurrencies": [16, 64], "requests_per_concurrency": 50}


class RecordingEvaluator(Evaluator):
    """Passes at once, and remembers which benchmark each run was sent to."""

    name = "recording"

    def __init__(self):
        self.slugs: list[str | None] = []

    def start(self, endpoint_url, served_model_name, context) -> str:
        self.slugs.append(context.get("benchmark_slug"))
        return f"rec:{len(self.slugs)}"

    def poll(self, external_ref) -> EvalOutcome:
        return EvalOutcome(status=EvalStatus.PASSED, metrics={"score_total": 7.0})


@pytest.fixture
def llmbench(monkeypatch):
    """LLMBench's side of creating a described benchmark: takes every spec,
    and counts how often it was asked."""
    asked: list[str] = []

    def ensure(spec):
        asked.append(spec.slug)
        return Ensured(slug=spec.slug, benchmark_id=len(asked), created=True, locked=True)

    monkeypatch.setattr(policy_sessions, "ensure_spec", ensure)
    return asked


async def _measured(supervisor, policy, bench_id) -> dict:
    for _ in range(4):
        supervisor.tick()
    out = (await policy.get(f"/benchmarks/{bench_id}")).json()
    assert out["status"] == "succeeded", out
    return out


async def test_a_probe_runs_on_the_benchmark_it_describes(night, llmbench):
    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)
    supervisor.bench = RecordingEvaluator()
    launch = await _serving_launch(supervisor, policy)

    asked = await policy.post(f"/launches/{launch}/benchmarks", json={"benchmark": QUICK})

    assert asked.status_code == 201, asked.text
    slug = BenchmarkSpec(**QUICK).slug
    assert asked.json()["suite"] == "probe"
    assert asked.json()["benchmark_slug"] == slug
    assert llmbench == [slug], "the benchmark is created when it is asked for"

    out = await _measured(supervisor, policy, asked.json()["id"])
    assert supervisor.bench.slugs == [slug]
    assert out["metrics"] == {"score_total": 7.0}
    assert out["summary"] is None, "nothing judges a probe"

    async with http.db() as session:
        events = (
            await session.execute(select(Event).where(Event.kind == "policy_bench_requested"))
        ).scalars().all()
        assert events[-1].payload["probe"] == slug


async def test_a_probes_result_is_kept_apart_from_the_campaigns(night, llmbench):
    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)
    supervisor.bench = RecordingEvaluator()
    launch = await _serving_launch(supervisor, policy)
    probe = await policy.post(f"/launches/{launch}/benchmarks", json={"benchmark": QUICK})
    await _measured(supervisor, policy, probe.json()["id"])

    async with http.db() as session:
        results = (await session.execute(select(Result))).scalars().all()
        by_source = {r.source: r for r in results}
        assert "llmbench" not in by_source, "it is not one of the campaign's results"
        assert by_source["probe"].metrics == {"score_total": 7.0}
        assert by_source["probe"].objective_value is None
        assert not (await session.execute(select(PolicyTrial))).scalars().all(), (
            "a probe is not a trial"
        )
        campaign = await session.get(Campaign, 1)
        assert "bench_screen" not in (campaign.run_timing or {}), (
            "a probe's length says nothing about the campaign's benchmark"
        )


async def test_a_quick_probe_then_a_longer_one_then_the_campaigns_own(night, llmbench):
    """What the feature is for: one engine, loaded once, measured three ways,
    each asked for after the one before came back."""
    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)
    supervisor.bench = RecordingEvaluator()
    launch = await _serving_launch(supervisor, policy)

    quick = await policy.post(f"/launches/{launch}/benchmarks", json={"benchmark": QUICK})
    await _measured(supervisor, policy, quick.json()["id"])
    longer = await policy.post(f"/launches/{launch}/benchmarks", json={"benchmark": LONG})
    await _measured(supervisor, policy, longer.json()["id"])
    own = await policy.post(f"/launches/{launch}/benchmarks", json={"suite": "screen"})
    judged = await _measured(supervisor, policy, own.json()["id"])

    assert supervisor.bench.slugs[:2] == [BenchmarkSpec(**QUICK).slug, BenchmarkSpec(**LONG).slug]
    assert supervisor.bench.slugs[2] not in supervisor.bench.slugs[:2]
    assert judged["suite"] == "screen" and judged["benchmark_slug"] == ""
    assert judged["summary"]["objective_value"] == 7.0, "the campaign's own is judged"
    assert (await policy.get(f"/launches/{launch}")).json()["status"] == "serving"
    async with http.db() as session:
        trials = (await session.execute(select(PolicyTrial))).scalars().all()
        assert [t.run_id for t in trials] == [own.json()["id"]]
        campaign = await session.get(Campaign, 1)
        assert len(campaign.run_timing["bench_screen"]) == 1


async def test_a_probes_results_are_served_in_full(night, llmbench):
    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)
    supervisor.bench = RecordingEvaluator()
    launch = await _serving_launch(supervisor, policy)
    probe = await policy.post(f"/launches/{launch}/benchmarks", json={"benchmark": QUICK})
    await _measured(supervisor, policy, probe.json()["id"])

    full = await policy.get(f"/benchmarks/{probe.json()['id']}/results")
    assert full.status_code == 200, full.text
    raw = await policy.get(f"/benchmarks/{probe.json()['id']}/results/raw")
    assert raw.status_code == 200, raw.text


async def test_a_retried_request_is_the_same_probe(night, llmbench):
    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)
    launch = await _serving_launch(supervisor, policy)
    headers = {"Idempotency-Key": "probe-1"}

    first = await policy.post(
        f"/launches/{launch}/benchmarks", json={"benchmark": QUICK}, **headers
    )
    again = await policy.post(
        f"/launches/{launch}/benchmarks", json={"benchmark": QUICK}, **headers
    )

    assert first.json()["id"] == again.json()["id"]
    assert len(llmbench) == 1, "the retry did not go back to LLMBench"
    assert again.json()["suite"] == "probe"


async def test_a_description_llmbench_will_not_take_is_refused_with_no_run(night, monkeypatch):
    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)
    launch = await _serving_launch(supervisor, policy)

    def refuse(spec):
        raise BenchmarkRefused("that slug is somebody else's")

    monkeypatch.setattr(policy_sessions, "ensure_spec", refuse)
    refused = await policy.post(f"/launches/{launch}/benchmarks", json={"benchmark": QUICK})
    assert refused.status_code == 409
    assert "somebody else's" in refused.text

    def unreachable(spec):
        raise ConnectionError("no route to host")

    monkeypatch.setattr(policy_sessions, "ensure_spec", unreachable)
    down = await policy.post(f"/launches/{launch}/benchmarks", json={"benchmark": QUICK})
    assert down.status_code == 502

    async with http.db() as session:
        benchmarks = (
            await session.execute(select(Run).where(Run.launch_run_id == launch))
        ).scalars().all()
        assert not benchmarks


async def test_a_description_that_makes_no_sense_is_a_422(night, llmbench):
    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)
    launch = await _serving_launch(supervisor, policy)

    bad = await policy.post(
        f"/launches/{launch}/benchmarks",
        json={"benchmark": {"kind": "sweep", "concurrencies": []}},
    )

    assert bad.status_code == 422
    assert not llmbench


async def test_an_unknown_suite_is_still_refused_without_a_description(night, llmbench):
    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)
    launch = await _serving_launch(supervisor, policy)

    refused = await policy.post(f"/launches/{launch}/benchmarks", json={"suite": "nightly"})

    assert refused.status_code == 422


async def test_a_self_served_engine_can_be_probed_too(night, llmbench):
    supervisor, driver, http = night
    policy = await _boot(supervisor, driver, http)
    supervisor.bench = RecordingEvaluator()
    manifest = (await policy.get("/session")).json()
    port = manifest["hardware"]["ports"][0]

    asked = await policy.post(
        "/benchmarks",
        json={"port": port, "engine_args": {"tp_size": 2}, "gpu_indices": [0, 1],
              "benchmark": QUICK},
    )

    assert asked.status_code == 201, asked.text
    out = await _measured(supervisor, policy, asked.json()["id"])
    assert out["suite"] == "probe"
    assert supervisor.bench.slugs == [BenchmarkSpec(**QUICK).slug]


async def test_a_probe_may_name_a_benchmark_the_campaign_allows(night, llmbench):
    """(1.11) An existing benchmark by slug, when the campaign lists it: run
    as a probe, created nowhere. Any other slug is refused, with no run."""
    supervisor, driver, http = night
    async with http.db() as session:
        campaign = await session.get(Campaign, 1)
        settings = campaign.policy_settings or {}
        campaign.policy_settings = {**settings, "benchmark_slugs": ["final-modules"]}
        # A campaign may be created with it: the settings schema takes it.
        PolicySettings.model_validate(campaign.policy_settings)
        await session.commit()
    policy = await _boot(supervisor, driver, http)
    supervisor.bench = RecordingEvaluator()
    launch = await _serving_launch(supervisor, policy)

    bench = f"/launches/{launch}/benchmarks"
    refused = await policy.post(bench, json={"benchmark_slug": "another"})
    assert refused.status_code == 422 and "final-modules" in refused.text
    asked = await policy.post(bench, json={"benchmark_slug": "final-modules"})
    assert asked.status_code == 201, asked.text
    assert asked.json()["suite"] == "probe"
    assert asked.json()["benchmark_slug"] == "final-modules"
    assert llmbench == [], "an existing benchmark is not created"
    out = await _measured(supervisor, policy, asked.json()["id"])
    assert supervisor.bench.slugs == ["final-modules"]
    assert out["summary"] is None, "nothing judges a probe"
    async with http.db() as session:
        runs = (await session.execute(select(Run))).scalars().all()
        assert sum(1 for r in runs if r.benchmark_slug == "final-modules") == 1
