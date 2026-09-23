"""AutoTune's own benchmarks on LLMBench: created once, then never borrowed.

A campaign compares candidates measured hours apart; that comparison is only
fair while the benchmark behind it stays put. So AutoTune measures with a
benchmark it created and locked — and it must never take over one somebody
else made, however convenient the slug.
"""

import json

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from app.evaluation.benchmarks import (
    DEFAULT_SCREEN_TEMPLATE,
    BenchmarkRefused,
    ensure_benchmark,
    load_template,
    template_names,
)
from app.evaluation.llmbench import LLMBenchClient
from app.mocks import llmbench_mock

ME = 7


class FakeLLMBench:
    """The four routes ensure uses, with LLMBench's semantics: an import makes
    the caller the creator, and the lock route toggles."""

    def __init__(self, existing: dict | None = None):
        self.benchmarks: dict[str, dict] = {}
        self.calls: list[str] = []
        if existing:
            self.benchmarks[existing["slug"]] = existing

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        self.calls.append(f"{method} {path}")
        if path == "/auth/me":
            return httpx.Response(200, json={"id": ME, "role": "service"})
        if method == "GET" and path.startswith("/benchmarks/"):
            found = self.benchmarks.get(path.rsplit("/", 1)[1])
            return httpx.Response(200, json=found) if found else httpx.Response(404)
        if path == "/benchmarks/admin/benchmarks/import":
            body = request.content.decode()
            doc = yaml.safe_load(body[body.index("slug:"):body.rindex("\r\n--")])
            row = {"id": 40 + len(self.benchmarks), "slug": doc["slug"], "is_locked": False,
                   "created_by_user_id": ME, "modules": doc["modules"]}
            self.benchmarks[doc["slug"]] = row
            return httpx.Response(201, json=row)
        if path.endswith("/lock"):
            bid = int(path.split("/")[-2])
            row = next(b for b in self.benchmarks.values() if b["id"] == bid)
            row["is_locked"] = not row["is_locked"]
            return httpx.Response(200, json=row)
        return httpx.Response(404)


def _client(fake) -> LLMBenchClient:
    return LLMBenchClient(base_url="http://llmbench", api_key="llmb_test",
                          transport=httpx.MockTransport(fake), max_attempts=1)


def test_a_missing_benchmark_is_created_from_the_template_and_locked():
    fake = FakeLLMBench()
    ensured = ensure_benchmark(_client(fake))
    assert (ensured.slug, ensured.created, ensured.locked) == (DEFAULT_SCREEN_TEMPLATE, True, True)
    row = fake.benchmarks[DEFAULT_SCREEN_TEMPLATE]
    assert [m["module_name"] for m in row["modules"]] == ["perf_guidellm_sweep"]


def test_a_benchmark_it_already_owns_is_used_as_is():
    fake = FakeLLMBench({"id": 3, "slug": DEFAULT_SCREEN_TEMPLATE, "is_locked": True,
                         "created_by_user_id": ME})
    ensured = ensure_benchmark(_client(fake))
    assert (ensured.benchmark_id, ensured.created) == (3, False)
    # No import, and no lock call: the toggle would have unlocked it.
    assert not any("import" in c or c.endswith("/lock") for c in fake.calls)


def test_its_own_benchmark_found_unlocked_is_locked_again():
    fake = FakeLLMBench({"id": 3, "slug": DEFAULT_SCREEN_TEMPLATE, "is_locked": False,
                         "created_by_user_id": ME})
    assert ensure_benchmark(_client(fake)).locked is True


def test_a_slug_someone_else_created_is_refused_and_left_alone():
    fake = FakeLLMBench({"id": 3, "slug": DEFAULT_SCREEN_TEMPLATE, "is_locked": False,
                         "created_by_user_id": ME + 1})
    with pytest.raises(BenchmarkRefused, match="choose another slug"):
        ensure_benchmark(_client(fake))
    assert fake.benchmarks[DEFAULT_SCREEN_TEMPLATE]["is_locked"] is False


def test_the_slug_can_be_overridden_and_is_checked():
    fake = FakeLLMBench()
    assert ensure_benchmark(_client(fake), slug="team-a-screen").slug == "team-a-screen"
    with pytest.raises(ValueError):
        ensure_benchmark(_client(fake), slug="Not A Slug")
    with pytest.raises(LookupError):
        load_template("../etc/passwd")


def test_every_template_is_in_llmbench_export_format():
    for name in template_names():
        doc = load_template(name)
        assert doc["slug"] == name
        for module in doc["modules"]:
            assert {"module_name", "params"} <= set(module)
            json.dumps(module["params"])          # plain data, importable as JSON params


def test_the_mock_llmbench_answers_ensure_like_the_real_one():
    """The mock stack's bootstrap ensures the screen benchmark; the mock must
    speak the same routes, or every mock run would log a failed startup."""
    with TestClient(llmbench_mock.app) as http:
        client = LLMBenchClient(base_url="http://testserver", api_key="llmb_test",
                                transport=http._transport, max_attempts=1)
        assert ensure_benchmark(client).created is False       # listed as its own
        fresh = ensure_benchmark(client, slug="mock-fresh-screen")
        assert fresh.created and fresh.locked
        assert "mock-fresh-screen" in {b["slug"] for b in client.list_benchmarks()}
    llmbench_mock.BENCHMARKS.pop("mock-fresh-screen", None)
    llmbench_mock._locked.discard("mock-fresh-screen")
