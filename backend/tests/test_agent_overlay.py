"""What a plugin knows about a run reaches its agent documents.

A plugin that starts runs for requests of its own (Plugin.run_overlay)
describes them: where they came from, how they are named, the metrics it
harvested, the group they compare within. A plugin may also accept its own
run selectors (Plugin.run_selector).
"""

import pytest

from app import plugins
from app.agent.overlay import RunOverlay
from app.plugins import PLUGIN_API_VERSION, Plugin, SelectorRefused
from app.schemas.agent import RunGroup
from tests.test_agent_api import _seed_five, stack  # noqa: F401  (fixture)

GROUP = RunGroup(kind="board", slug="glm-h100", name="GLM on H100", model_name="GLM-5.2",
                 precision="fp8", gpu_type="H100")


def _describing(overlays: dict[int, RunOverlay], selector=None) -> Plugin:
    return Plugin(
        name="boards", api_version=PLUGIN_API_VERSION,
        run_overlay=lambda session, run, campaign: overlays.get(run.id),
        run_selector=selector,
    )


@pytest.fixture
def enable(monkeypatch):
    def _enable(plugin: Plugin) -> None:
        monkeypatch.setattr(plugins, "enabled", lambda: (plugin,))

    return _enable


async def test_a_plugin_describes_where_a_run_came_from(stack, enable):  # noqa: F811
    http, factory = stack
    ids = await _seed_five(factory)
    enable(_describing({ids["D"]: RunOverlay(
        source="request", origin={"request_id": 7, "notes": "plus fp8 kv cache"},
        label="kv fp8 v2", group=GROUP,
        metrics={"perf_guidellm_sweep.extra_metric": 1.5},
    )}))

    doc = (await http.get(f"/api/agent/v1/runs/{ids['D']}")).json()
    assert doc["launch"]["origin"]["source"] == "request"
    assert doc["launch"]["origin"]["extensions"] == {"request_id": 7, "notes": "plus fp8 kv cache"}
    assert doc["group"]["slug"] == "glm-h100"
    assert doc["results"]["metrics"]["perf_guidellm_sweep.extra_metric"] == 1.5

    plain = (await http.get(f"/api/agent/v1/runs/{ids['A']}")).json()
    assert plain["launch"]["origin"]["source"] == "campaign" and plain["group"] is None


async def test_runs_of_one_group_compare_by_the_group(stack, enable):  # noqa: F811
    http, factory = stack
    ids = await _seed_five(factory)
    other = GROUP.model_copy(update={"slug": "glm-h200"})
    enable(_describing({
        ids["A"]: RunOverlay(group=GROUP),
        ids["C"]: RunOverlay(group=GROUP, label="C mem"),
        ids["D"]: RunOverlay(group=other),
    }))

    same = (await http.get("/api/agent/v1/comparison",
                           params={"baseline": ids["A"], "attempts": ids["C"]})).json()
    assert same["comparable"] is True and same["group"]["slug"] == "glm-h100"
    assert same["attempts"][0]["label"] == "C mem"

    differ = await http.get("/api/agent/v1/comparison",
                            params={"baseline": ids["A"], "attempts": ids["D"]})
    assert differ.status_code == 422
    reasons = differ.json()["detail"]["reasons"]
    assert [r["code"] for r in reasons] == ["group_differs"]
    assert reasons[0]["field"] == "board" and reasons[0]["attempt"] == "glm-h200"


async def test_a_plugin_may_accept_its_own_run_selectors(stack, enable):  # noqa: F811
    http, factory = stack
    ids = await _seed_five(factory)

    def selector(session, token):
        if token == "req:4":
            return ids["D"]
        if token == "req:9":
            raise SelectorRefused("request_has_no_run", 409, "not launched yet")
        return None

    enable(_describing({}, selector=selector))

    doc = (await http.get("/api/agent/v1/comparison",
                          params={"baseline": ids["A"], "attempts": "req:4"})).json()
    assert doc["attempts"][0]["run_id"] == ids["D"]

    refused = await http.get("/api/agent/v1/comparison",
                             params={"baseline": ids["A"], "attempts": "req:9"})
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"] == "request_has_no_run"

    unknown = await http.get("/api/agent/v1/comparison",
                             params={"baseline": ids["A"], "attempts": "nope"})
    assert unknown.status_code == 422 and unknown.json()["detail"]["error"] == "bad_selector"


async def test_a_saved_report_is_filed_under_its_group(stack, enable):  # noqa: F811
    http, factory = stack
    ids = await _seed_five(factory)
    enable(_describing({ids["A"]: RunOverlay(group=GROUP), ids["C"]: RunOverlay(group=GROUP)}))
    saved = await http.post("/api/agent/v1/reports", json={
        "title": "t", "baseline_run_id": ids["A"], "attempt_run_ids": [ids["C"]],
        "markdown": "# t", "comparable": True,
    })
    assert saved.status_code == 201, saved.text
    assert saved.json()["group"]["slug"] == "glm-h100"
    listed = (await http.get("/api/agent/v1/reports", params={"group": "glm-h100"})).json()
    assert [r["id"] for r in listed] == [saved.json()["id"]]
    assert (await http.get("/api/agent/v1/reports", params={"group": "other"})).json() == []
