"""Starting a campaign without retyping it: spec, clone, draft.

A campaign is ~30 fields, most of which the platform already knows — the model's
production launch (its baseline), the fleet's cards, the benchmark's dataset.
Three things let an author start from that knowledge instead of a blank form,
and each has a way to go quietly wrong:

- the **spec** (export/import, clone) silently drops a field it does not list,
  and a policy campaign comes back as an enumerating one;
- a **clone** that skipped the create path's validation would save what a form
  would have refused;
- a **draft** that guessed a value without saying so would be reviewed as if
  someone had chosen it.
"""

import itertools
import re
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.campaign_spec import (
    MAX_DRAFT_CANDIDATES,
    SPEC_KEYS,
    DraftRequest,
    draft_campaign,
    spec_of,
)
from app.control.search.space import candidate_count
from app.db.base import Base, get_async_session
from app.db.models import Baseline, Campaign, LeaseState, Machine, Objective, User
from app.main import app
from app.schemas.core import CampaignCreate

FRONTEND_YAML = Path(__file__).resolve().parents[2] / "frontend/src/utils/campaignYaml.ts"


# --- the spec ----------------------------------------------------------------------


def test_the_yaml_editor_carries_every_field_a_campaign_is_defined_by():
    """The frontend's list and the server's are two copies of one fact; an
    export that drops `policy_id` re-imports a policy campaign as one that
    enumerates, and nothing says so."""
    source = FRONTEND_YAML.read_text()
    block = re.search(r"PORTABLE_KEYS[^=]*=\s*\[(.*?)\]", source, re.S).group(1)
    frontend = set(re.findall(r"'([a-z_]+)'", block))
    assert frontend == set(SPEC_KEYS)


def test_a_spec_is_exactly_the_body_that_recreates_the_campaign():
    campaign = Campaign(
        id=1, owner_id=1, name="c", engine="sglang", image="img", model_path="/m",
        served_model_name="m", search_space={"grid": {"tp": [2, 4]}},
        objective={"target_metric": "x"}, policy_id=3, policy_settings={"max_contenders": 2},
        node_group="pair", deploy_branch="main", auto_promote=True,
        machine_names=[], extra_env={}, extra_volumes={}, verify_objective={},
        benchmark_slug="", service_port=28200, run_baseline_canary=True, share_machine=True,
        daily_start="", daily_end="", schedule_timezone="", max_run_minutes=150,
        confirm_top_k=0, confirm_repeats=3, verify_benchmark_slug="", verify_top_k=0,
        verify_max_run_minutes=180, dataset_profile="", dataset_policy="rebuild_at_start",
    )
    body = CampaignCreate.model_validate(spec_of(campaign))
    assert body.policy_id == 3 and body.node_group == "pair" and body.auto_promote is True
    assert "window_start" not in spec_of(campaign)   # runtime, not definition


# --- drafting ------------------------------------------------------------------------


def _db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _leased(session, name="gpu-a", cards=8, card_type="H100"):
    session.add(Machine(name=name, host="198.51.100.10", gpu_count=cards, gpu_type=card_type,
                        lease_state=LeaseState.ACTIVE.value))


def test_a_draft_from_a_baseline_serves_what_production_serves_and_searches_around_it():
    factory = _db()
    with factory() as s:
        _leased(s)
        s.add(Baseline(id=5, served_model_name="qwen", engine="sglang", card_type="H100",
                       image="lmsysorg/sglang:v1", model_path="/models/qwen",
                       engine_args={"tp": 4, "mem_fraction_static": 0.88},
                       source="capture:gpu-a"))
        s.commit()
        draft = draft_campaign(s, DraftRequest(baseline_id=5))

    c = draft.campaign
    assert (c["image"], c["model_path"], c["served_model_name"]) == (
        "lmsysorg/sglang:v1", "/models/qwen", "qwen")
    # Production's width and its neighbours, within the 8 cards the fleet has —
    # and tp is swept, so it is no longer pinned in the base.
    assert c["search_space"]["grid"] == {"tp": [2, 4, 8]}
    assert c["search_space"]["base"] == {"mem_fraction_static": 0.88}
    assert c["machine_names"] == ["gpu-a"]
    # Every derived value says where it came from.
    assert "baseline #5" in draft.provenance["image"]
    assert "production's 4" in draft.provenance["search_space.grid"]
    assert draft.warnings == []
    CampaignCreate.model_validate(c)


def test_the_baseline_is_found_by_model_and_the_fleets_card_type():
    factory = _db()
    with factory() as s:
        _leased(s, card_type="A100")
        s.add(Baseline(id=1, served_model_name="qwen", engine="sglang", card_type="H100",
                       image="h100-image", model_path="/m"))
        s.add(Baseline(id=2, served_model_name="qwen", engine="sglang", card_type="A100",
                       image="a100-image", model_path="/m"))
        s.commit()
        draft = draft_campaign(s, DraftRequest(served_model_name="qwen"))
    assert draft.campaign["image"] == "a100-image"


def test_without_a_baseline_the_draft_says_what_is_missing_and_stays_one_nights_work():
    factory = _db()
    with factory() as s:
        _leased(s, cards=4)
        s.commit()
        draft = draft_campaign(s, DraftRequest(served_model_name="new-model"))
    assert draft.campaign["image"] == "" and draft.campaign["model_path"] == ""
    assert any("no baseline" in w for w in draft.warnings)
    assert max(draft.campaign["search_space"]["grid"]["tp"]) <= 4
    assert candidate_count(draft.campaign["search_space"]) <= MAX_DRAFT_CANDIDATES


def test_a_verify_stage_pins_the_dataset_its_benchmark_actually_replays():
    factory = _db()
    with factory() as s:
        _leased(s)
        s.add(Baseline(id=1, served_model_name="m", engine="sglang", image="i", model_path="/m"))
        s.commit()
        draft = draft_campaign(
            s, DraftRequest(baseline_id=1, verify_benchmark_slug="replay-suite-v1"),
            dataset_wiring={"replay-suite-v1": "prod-traffic-sample"},
        )
    assert draft.campaign["dataset_profile"] == "prod-traffic-sample"
    assert draft.campaign["verify_top_k"] == 3
    assert "replays on LLMBench" in draft.provenance["dataset_profile"]


def test_a_draft_that_could_not_ask_llmbench_says_so():
    factory = _db()
    with factory() as s:
        s.add(Baseline(id=1, served_model_name="m", engine="sglang", image="i", model_path="/m"))
        s.commit()
        draft = draft_campaign(s, DraftRequest(baseline_id=1, verify_benchmark_slug="v"),
                               dataset_wiring=None)
    assert any("could not be asked" in w for w in draft.warnings)
    assert any("no leased machine" in w for w in draft.warnings)


# --- through the API: draft, clone, duplicate ----------------------------------------------

_DB = itertools.count()


@pytest.fixture
async def api():
    engine = create_async_engine(
        f"sqlite+aiosqlite:///file:spec{next(_DB)}?mode=memory&cache=shared&uri=true")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_async_session] = override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t/api") as http:
        r = await http.post("/auth/register", json={"username": "author", "password": "secret123"})
        assert r.status_code == 200, r.text
        http.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
        yield http, factory
    app.dependency_overrides.clear()
    await engine.dispose()


async def test_a_posted_draft_creates_the_campaign_it_described(api):
    http, factory = api
    async with factory() as s:
        s.add(Baseline(id=1, served_model_name="m", engine="sglang", image="i", model_path="/m",
                       engine_args={"tp": 2}))
        s.add(Machine(name="gpu-a", host="h", gpu_count=8, gpu_type="H100",
                      lease_state=LeaseState.ACTIVE.value))
        await s.commit()
    draft = await http.post("/campaigns/draft", json={"baseline_id": 1})
    assert draft.status_code == 200, draft.text
    created = await http.post("/campaigns", json=draft.json()["campaign"])
    assert created.status_code == 200, created.text
    assert created.json()["search_space"]["grid"] == {"tp": [1, 2, 4]}


async def test_a_clone_copies_the_definition_and_takes_overrides(api):
    http, factory = api
    async with factory() as s:
        s.add(Campaign(id=9, owner_id=1, name="orig", engine="sglang", image="i",
                       model_path="/m", served_model_name="m",
                       search_space={"grid": {"tp": [2]}}, node_group="",
                       deploy_branch="release", auto_promote=True))
        await s.commit()
    r = await http.post("/campaigns/9/clone", json={
        "name": "copy", "overrides": {"search_space": {"grid": {"tp": [2, 4]}}}})
    assert r.status_code == 200, r.text
    copy = r.json()
    assert copy["name"] == "copy" and copy["id"] != 9
    assert copy["deploy_branch"] == "release" and copy["auto_promote"] is True
    assert copy["search_space"]["grid"] == {"tp": [2, 4]}
    assert copy["status"] in ("draft", "scheduled")


async def test_a_clone_is_validated_like_a_new_campaign(api):
    http, factory = api
    async with factory() as s:
        s.add(Campaign(id=9, owner_id=1, name="orig", engine="sglang", image="i",
                       model_path="/m", served_model_name="m", search_space={"grid": {"tp": [2]}}))
        await s.commit()
    bad = await http.post("/campaigns/9/clone",
                          json={"name": "x", "overrides": {"engine": "tgi"}})
    assert bad.status_code == 422
    unknown = await http.post("/campaigns/9/clone",
                              json={"name": "x", "overrides": {"window_start": "2026-01-01"}})
    assert unknown.status_code == 422 and "not campaign fields" in unknown.text


async def test_a_built_in_objective_is_duplicated_into_an_editable_one(api):
    http, factory = api
    async with factory() as s:
        s.add(User(id=99, username="system", password_hash="x"))
        s.add(Objective(id=4, owner_id=99, name="Throughput", target_metric="a.b",
                        direction="maximize", redlines=[{"metric": "a.c", "op": ">=", "value": 1}],
                        is_builtin=True))
        await s.commit()
    first = await http.post("/objectives/4/duplicate", json={})
    second = await http.post("/objectives/4/duplicate", json={})
    assert first.status_code == 200, first.text
    assert (first.json()["name"], second.json()["name"]) == (
        "Throughput (copy)", "Throughput (copy 2)")
    assert first.json()["is_builtin"] is False
    assert first.json()["redlines"] == [{"metric": "a.c", "op": ">=", "value": 1}]
