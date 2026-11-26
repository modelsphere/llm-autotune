"""Replay datasets set up from the Datasets page.

A dataset is a collection profile on LLMBench. The form asks for little and
fills in the rest; the platform takes a new sample on the dataset's interval,
but never under a campaign that is measuring on the current one.
"""

from datetime import UTC, datetime, timedelta
from itertools import count

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.api import datasets as datasets_api
from app.control.orchestrator.supervisor import Supervisor
from app.core.auth import create_token
from app.core.config import get_settings
from app.datasets import editor, refresh
from app.datasets.profiles import BuildInFlight, DatasetBuild, DatasetProfileError
from app.db.base import Base, get_async_session
from app.db.models import Campaign, CampaignStatus, Event, ReplayDataset, User
from app.main import app

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
ACCOUNT = 7  # this platform's account on LLMBench


def _profile(name="qwen3-8b", pid=1, *, built_at=None, last=None, owner=ACCOUNT, **extra):
    current = None
    if built_at is not None:
        current = {"build_id": f"b-{built_at:%H%M}", "sha256": "ab" * 32, "records": 2000,
                   "built_at": built_at.isoformat()}
    return {"id": pid, "name": name, "display_name": name, "created_by_user_id": owner,
            "models": ["qwen3-8b"], "source_type": "victorialogs",
            "source_url": "http://victorialogs:9428", "current": current,
            "last_build": last, "schedule_interval_hours": 0, **extra}


# -- the form ------------------------------------------------------------------


def test_names_are_made_from_what_the_person_typed():
    assert editor.slug_for("Qwen3-8B production") == "qwen3-8b-production"
    assert editor.slug_for("meta-llama/Llama-3.1-8B-Instruct") == "meta-llama-llama-3-1-8b-instruct"
    assert editor.slug_for("!!!") == "dataset"
    assert editor.unused_slug("qwen3-8b", {"qwen3-8b", "qwen3-8b-2"}) == "qwen3-8b-3"
    assert editor.valid_slug("qwen3-8b") and not editor.valid_slug("Qwen3")


def test_a_form_becomes_a_manual_victorialogs_profile():
    form = editor.DatasetForm(model=" qwen3-8b ", forwarded_to=["192.0.2.11", " "])
    fields = editor.profile_fields(form, "http://victorialogs:9428")
    assert fields["source_type"] == "victorialogs"
    assert fields["models"] == ["qwen3-8b"] and fields["forwarded_to"] == ["192.0.2.11"]
    assert fields["display_name"] == "qwen3-8b"
    # Samples are taken by this platform, never on LLMBench's own clock.
    assert fields["schedule_interval_hours"] == 0 and fields["max_age_hours"] == 0
    assert fields["sample_size"] == 2000 and fields["window_hours"] == 24
    assert editor.form_of({**fields, "name": "qwen3-8b"}, 24)["model"] == "qwen3-8b"


# -- when a new sample is due -------------------------------------------------------


def test_a_sample_is_due_one_interval_after_the_last():
    fresh = _profile(built_at=NOW - timedelta(hours=3))
    assert refresh.due_at(24, fresh, NOW) == NOW + timedelta(hours=21)
    assert not refresh.is_due(24, fresh, NOW)
    assert refresh.is_due(2, fresh, NOW)
    assert refresh.due_at(0, fresh, NOW) is None          # only when asked
    assert refresh.is_due(24, _profile(), NOW)            # never sampled: now


def test_a_build_in_flight_or_a_recent_failure_is_not_asked_again():
    old = NOW - timedelta(hours=30)
    running = _profile(built_at=old, last={"status": "running"})
    assert not refresh.is_due(24, running, NOW)
    failed = _profile(built_at=old, last={"status": "failed",
                                          "finished_at": (NOW - timedelta(hours=1)).isoformat()})
    assert refresh.due_at(24, failed, NOW) == NOW + timedelta(hours=23)


# -- the supervisor takes the samples ---------------------------------------------------


class FakeProfiles:
    def __init__(self, profiles):
        self.profiles = profiles
        self.triggered: list[int] = []
        self.lists = 0

    def list_profiles(self):
        self.lists += 1
        return self.profiles

    def current(self, profile):
        return DatasetBuild.from_api((profile or {}).get("current") or {})

    def trigger_build(self, profile_id):
        self.triggered.append(profile_id)
        return 50 + profile_id


def _supervisor(profiles, *, refresh_hours=24, campaign_build=None, campaign_status="active"):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add(User(id=1, username="u", password_hash="x"))
        session.add(ReplayDataset(name="qwen3-8b", refresh_hours=refresh_hours, created_by_id=1))
        if campaign_build is not None:
            session.add(Campaign(id=1, owner_id=1, name="night", engine="sglang", image="img",
                                 model_path="/m", served_model_name="m",
                                 search_space={"grid": {"tp": [1]}}, status=campaign_status,
                                 dataset_profile="qwen3-8b", dataset_build_id=campaign_build))
        session.commit()
    sup = Supervisor(session_factory=factory, dataset_profiles=FakeProfiles(profiles))
    return sup, factory


def _refresh(sup, factory):
    with factory() as session:
        sup._refresh_datasets(session)
        session.commit()
        return [e.kind for e in session.scalars(select(Event))]


def test_a_dataset_due_a_sample_gets_one():
    profile = _profile(built_at=datetime.now(UTC) - timedelta(hours=25))
    sup, factory = _supervisor([profile])
    assert _refresh(sup, factory) == ["dataset_refresh_requested"]
    assert sup.datasets.triggered == [1]
    # Looked at again only after a few minutes, not every tick.
    _refresh(sup, factory)
    assert sup.datasets.lists == 1


def test_a_campaign_measuring_on_the_current_sample_holds_it():
    profile = _profile(built_at=datetime.now(UTC) - timedelta(hours=25))
    build = profile["current"]["build_id"]
    sup, factory = _supervisor([profile], campaign_build=build, campaign_status="paused")
    assert _refresh(sup, factory) == []
    assert sup.datasets.triggered == []

    done, factory = _supervisor([profile], campaign_build=build, campaign_status="done")
    _refresh(done, factory)
    assert done.datasets.triggered == [1]


def test_datasets_that_refresh_only_on_request_are_left_alone():
    sup, factory = _supervisor([_profile()], refresh_hours=0)
    assert _refresh(sup, factory) == []
    assert sup.datasets.lists == 0


# -- the API ----------------------------------------------------------------------------


class FakeBench:
    """The collection-profile API of LLMBench, kept in memory."""

    profiles: dict[str, dict] = {}
    calls: list[tuple] = []
    collector = True
    fail_trigger: Exception | None = None

    def list_with_collector(self):
        return list(self.profiles.values()), self.collector

    def list_profiles(self):
        return list(self.profiles.values())

    def account_id(self):
        return ACCOUNT

    def find(self, name):
        return self.profiles.get(name)

    def create(self, body):
        profile = {**body, "id": len(self.profiles) + 1, "created_by_user_id": ACCOUNT,
                   "current": None, "last_build": None}
        self.profiles[body["name"]] = profile
        self.calls.append(("create", body["name"]))
        return profile

    def update(self, pid, body):
        profile = next(p for p in self.profiles.values() if p["id"] == pid)
        profile.update(body)
        self.calls.append(("update", profile["name"]))
        return profile

    def delete(self, pid):
        name = next(n for n, p in self.profiles.items() if p["id"] == pid)
        del self.profiles[name]
        self.calls.append(("delete", name))

    def trigger_build(self, pid):
        if self.fail_trigger:
            raise self.fail_trigger
        self.calls.append(("build", pid))
        return 90 + pid

    def probe(self, body):
        self.calls.append(("probe", body["source_url"], tuple(body["models"])))
        return {"matched": 1957, "query": "q", "error": None}

    def values(self, body):
        self.calls.append(("values", body["field"], tuple(body["models"])))
        return {"values": [{"value": "qwen3-8b", "count": 1957}], "error": None}

    def builds(self, pid, limit=10):
        return [{"id": 1, "status": "ready", "records": 2000}]


_counter = count()


@pytest_asyncio.fixture
async def api(monkeypatch):
    FakeBench.profiles, FakeBench.calls = {}, []
    FakeBench.collector, FakeBench.fail_trigger = True, None
    monkeypatch.setattr(datasets_api, "DatasetProfileClient", FakeBench)
    monkeypatch.setattr(get_settings(), "llmbench_victorialogs_url", "http://victorialogs:9428")
    name = f"replay_datasets_{next(_counter)}"
    engine = create_async_engine(f"sqlite+aiosqlite:///file:{name}?mode=memory&cache=shared&uri=true")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _session():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_async_session] = _session
    async with factory() as session:
        session.add(User(id=1, username="owner", password_hash="x", role="user"))
        session.add(User(id=2, username="other", password_hash="x", role="user"))
        session.add(User(id=3, username="admin", password_hash="x", role="admin"))
        await session.commit()
        tokens = {u: create_token(await session.get(User, u)) for u in (1, 2, 3)}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t/api") as http:
        http.headers["Authorization"] = f"Bearer {tokens[1]}"
        http.as_other = {"Authorization": f"Bearer {tokens[2]}"}
        http.as_admin = {"Authorization": f"Bearer {tokens[3]}"}
        http.db = factory
        yield http
    app.dependency_overrides.clear()
    await engine.dispose()


async def test_a_dataset_is_set_up_from_a_model_alone(api):
    resp = await api.post("/datasets", json={"model": "qwen3-8b"})
    assert resp.status_code == 201, resp.text
    assert resp.json() == {"name": "qwen3-8b", "display_name": "qwen3-8b", "collect_error": ""}
    profile = FakeBench.profiles["qwen3-8b"]
    assert profile["source_url"] == "http://victorialogs:9428"
    assert profile["schedule_interval_hours"] == 0
    assert ("build", profile["id"]) in FakeBench.calls       # the first sample, straight away
    async with api.db() as session:
        row = await session.get(ReplayDataset, "qwen3-8b")
        assert row.refresh_hours == 24 and row.created_by_id == 1

    again = (await api.post("/datasets", json={"model": "qwen3-8b"})).json()
    assert again["name"] == "qwen3-8b-2"


async def test_the_list_says_who_may_edit_what(api):
    await api.post("/datasets", json={"model": "qwen3-8b", "refresh_hours": 12})
    FakeBench.profiles["prod"] = _profile("prod", pid=9, owner=1, schedule_interval_hours=24)
    mine = {d["name"]: d for d in (await api.get("/datasets")).json()["datasets"]}
    assert mine["qwen3-8b"]["can_edit"] and not mine["qwen3-8b"]["external"]
    assert mine["qwen3-8b"]["refresh_hours"] == 12
    assert mine["prod"]["external"] and not mine["prod"]["can_edit"]
    assert mine["prod"]["schedule_hours"] == 24

    theirs = {d["name"]: d for d in
              (await api.get("/datasets", headers=api.as_other)).json()["datasets"]}
    assert not theirs["qwen3-8b"]["can_edit"]
    body = {"model": "qwen3-8b", "sample_size": 500}
    assert (await api.put("/datasets/qwen3-8b", json=body, headers=api.as_other)).status_code == 403
    assert (await api.put("/datasets/qwen3-8b", json=body, headers=api.as_admin)).status_code == 200
    assert FakeBench.profiles["qwen3-8b"]["sample_size"] == 500


async def test_a_dataset_a_campaign_still_replays_is_not_deleted(api):
    await api.post("/datasets", json={"model": "qwen3-8b"})
    async with api.db() as session:
        session.add(Campaign(owner_id=1, name="night", engine="sglang", image="img",
                             model_path="/m",
                             served_model_name="m", search_space={"grid": {"tp": [1]}},
                             status=CampaignStatus.PAUSED.value, dataset_profile="qwen3-8b"))
        await session.commit()
    resp = await api.delete("/datasets/qwen3-8b")
    assert resp.status_code == 409 and "night" in resp.json()["detail"]
    async with api.db() as session:
        (await session.execute(select(Campaign))).scalar_one().status = CampaignStatus.DONE.value
        await session.commit()
    assert (await api.delete("/datasets/qwen3-8b")).status_code == 204
    assert "qwen3-8b" not in FakeBench.profiles


async def test_collecting_under_a_campaign_needs_force(api):
    FakeBench.profiles["qwen3-8b"] = _profile(built_at=NOW)
    async with api.db() as session:
        session.add(Campaign(owner_id=1, name="night", engine="sglang", image="img",
                             model_path="/m",
                             served_model_name="m", search_space={"grid": {"tp": [1]}},
                             status=CampaignStatus.ACTIVE.value, dataset_profile="qwen3-8b",
                             dataset_build_id=f"b-{NOW:%H%M}"))
        await session.commit()
    resp = await api.post("/datasets/qwen3-8b/collect")
    assert resp.status_code == 409 and "night" in resp.json()["detail"]
    assert (await api.post("/datasets/qwen3-8b/collect?force=true")).json() == {"build_row": 91}

    FakeBench.fail_trigger = BuildInFlight("busy")
    assert (await api.post("/datasets/qwen3-8b/collect?force=true")).status_code == 409


async def test_the_form_is_checked_against_the_log_store(api):
    out = (await api.post("/datasets/check", json={"model": "qwen3-8b", "window_hours": 72})).json()
    assert out["matched"] == 1957 and out["hours"] == 24
    nodes = (await api.post("/datasets/values",
                            json={"field": "forwarded_to", "model": "qwen3-8b"})).json()
    assert nodes["values"][0]["value"] == "qwen3-8b"
    assert ("values", "forwarded_to", ("qwen3-8b",)) in FakeBench.calls


async def test_without_a_log_store_the_form_asks_for_one(api, monkeypatch):
    monkeypatch.setattr(get_settings(), "llmbench_victorialogs_url", "")
    resp = await api.post("/datasets", json={"model": "qwen3-8b"})
    assert resp.status_code == 422 and "VictoriaLogs" in resp.json()["detail"]
    ok = await api.post("/datasets", json={"model": "qwen3-8b",
                                           "source_url": "http://vl.example:9428/"})
    assert ok.status_code == 201
    assert FakeBench.profiles["qwen3-8b"]["source_url"] == "http://vl.example:9428"


async def test_a_first_sample_that_cannot_start_is_reported_not_fatal(api):
    FakeBench.fail_trigger = DatasetProfileError("build refused (503): no collector")
    out = (await api.post("/datasets", json={"model": "qwen3-8b"})).json()
    assert "no collector" in out["collect_error"]
    assert "qwen3-8b" in FakeBench.profiles


@pytest.mark.parametrize("name", ["Qwen 3", "-x", "x" * 65])
async def test_an_id_that_cannot_be_a_profile_name_is_refused(api, name):
    resp = await api.post("/datasets", json={"model": "qwen3-8b", "name": name})
    assert resp.status_code == 422


async def test_the_list_carries_the_load_reading_of_the_sample(api):
    reading = {"version": 1, "status": "ok", "category": "moderate", "w_log": 0.22, "knee_group": 4}
    profile = _profile("prod", pid=9, owner=1, built_at=NOW)
    profile["current"]["load"] = reading
    FakeBench.profiles["prod"] = profile
    listed = {d["name"]: d for d in (await api.get("/datasets")).json()["datasets"]}
    assert listed["prod"]["current"]["load"] == reading


def test_a_timed_form_sends_its_layout_and_a_sampled_one_sends_nothing_new():
    timed_form = editor.DatasetForm(model="m", layout="timed", session_field="req_headers.x-s",
                                    max_idle_gap_s=3)
    fields = editor.profile_fields(timed_form, "http://victorialogs:9428")
    assert fields["layout"] == "timed" and fields["session_field"] == "req_headers.x-s"
    assert fields["max_idle_gap_s"] == 3
    sampled = editor.profile_fields(editor.DatasetForm(model="m"), "http://victorialogs:9428")
    assert not {"layout", "session_field", "max_idle_gap_s"} & set(sampled)
    back = editor.form_of({**fields, "name": "m"}, 0)
    assert back["layout"] == "timed" and back["max_idle_gap_s"] == 3
