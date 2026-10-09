"""Loading plugins, and what the platform guarantees around them.

The loader refuses, with a message that says why, anything it cannot load as
configured; a tick step that raises is rolled back without stopping the tick;
the platform's migration history never answers for a plugin's tables. The
example plugin under tests/plugins/ exercises the hooks end to end.
"""

import re
from importlib.metadata import EntryPoint

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app import plugins
from app.control.orchestrator.supervisor import Supervisor
from app.db.base import Base
from app.db.models import Event
from app.plugins import ENTRY_POINT_GROUP, PLUGIN_API_VERSION, Plugin, PluginError
from app.worker import _stamped
from tests.fakes import NullDriver, StubEvaluator

GOOD = Plugin(name="good", api_version=PLUGIN_API_VERSION)
OLD = Plugin(name="old", api_version=PLUGIN_API_VERSION - 1)
MISNAMED = Plugin(name="other", api_version=PLUGIN_API_VERSION)
BAD_NAME = Plugin(name="Bad-Name", api_version=PLUGIN_API_VERSION)
NOT_A_PLUGIN = {"name": "dict"}


def _installed(**targets: str) -> dict[str, EntryPoint]:
    return {
        name: EntryPoint(name=name, value=f"tests.test_plugins:{attr}", group=ENTRY_POINT_GROUP)
        for name, attr in targets.items()
    }


def test_loads_the_listed_plugins_in_order():
    installed = _installed(good="GOOD", other="MISNAMED")
    assert plugins.load(["good"], installed) == [GOOD]
    assert plugins.load([], installed) == []


def test_an_installed_plugin_stays_off_unless_listed(monkeypatch):
    monkeypatch.setattr(plugins.get_settings(), "plugins", "")
    assert plugins.configured_names() == []
    monkeypatch.setattr(plugins.get_settings(), "plugins", " good, other ,")
    assert plugins.configured_names() == ["good", "other"]


@pytest.mark.parametrize(
    ("names", "installed", "says"),
    [
        (["missing"], {"good": "GOOD"}, "no installed package provides it"),
        (["old"], {"old": "OLD"}, f"written for plugin API {PLUGIN_API_VERSION - 1}"),
        (["mis"], {"mis": "MISNAMED"}, "names itself 'other'"),
        (["dict"], {"dict": "NOT_A_PLUGIN"}, "is not an app.plugins.Plugin"),
        (["Bad-Name"], {"Bad-Name": "BAD_NAME"}, "lowercase letters"),
        (["good", "good"], {"good": "GOOD"}, "listed twice"),
        (["broken"], {"broken": "NO_SUCH_ATTRIBUTE"}, "failed to import"),
    ],
)
def test_refuses_what_it_cannot_load_and_says_why(names, installed, says):
    with pytest.raises(PluginError, match=_escape(says)):
        plugins.load(names, _installed(**installed))


def _escape(text_: str) -> str:
    return re.escape(text_)


# ------------------------------------------------------------------ tick steps


def _supervisor():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    supervisor = Supervisor(
        session_factory=factory,
        health_evaluator=StubEvaluator(),
        bench_evaluator=StubEvaluator(),
    )
    supervisor.driver = NullDriver()
    return supervisor, factory


def _note(kind):
    def step(supervisor, session):
        session.add(Event(actor="plugin", kind=kind, payload={}))
        session.flush()

    step.__name__ = f"note_{kind}"
    return step


def _fail_after_writing(supervisor, session):
    session.add(Event(actor="plugin", kind="half_done", payload={}))
    session.flush()
    raise RuntimeError("boom")


def test_tick_steps_run_and_a_failing_one_is_rolled_back_alone(monkeypatch, caplog):
    supervisor, factory = _supervisor()
    plugin = Plugin(
        name="steps",
        api_version=PLUGIN_API_VERSION,
        tick_steps=(_note("first"), _fail_after_writing, _note("second")),
    )
    monkeypatch.setattr(plugins, "enabled", lambda: (plugin,))

    supervisor.tick()

    with factory() as session:
        kinds = session.scalars(select(Event.kind).where(Event.actor == "plugin")).all()
    assert sorted(kinds) == ["first", "second"]  # half_done rolled back with its step
    assert "plugin steps: tick step _fail_after_writing failed" in caplog.text


def test_tick_steps_run_before_the_platforms_own_work(monkeypatch):
    supervisor, _ = _supervisor()
    order: list[str] = []
    monkeypatch.setattr(
        supervisor, "_advance_campaign_schedules", lambda session: order.append("platform")
    )
    plugin = Plugin(
        name="first",
        api_version=PLUGIN_API_VERSION,
        tick_steps=(lambda supervisor, session: order.append("plugin"),),
    )
    monkeypatch.setattr(plugins, "enabled", lambda: (plugin,))

    supervisor.tick()

    assert order == ["plugin", "platform"]


# ------------------------------------------------------------------ schema


def test_each_migration_history_compares_only_its_own_tables():
    own = MetaData()
    Table("plugin_rows", own, Column("id", Integer, primary_key=True))
    include = plugins.only_tables_of(own)

    platform_table = Base.metadata.tables["campaigns"]
    assert include(own.tables["plugin_rows"], "plugin_rows", "table", False, None)
    assert not include(platform_table, "campaigns", "table", True, None)
    assert not include(None, "alembic_version_x", "table", True, None)
    column = own.tables["plugin_rows"].c.id
    assert include(column, "id", "column", False, None)

    core = plugins.only_tables_of(Base.metadata)
    assert core(platform_table, "campaigns", "table", True, None)
    assert not core(own.tables["plugin_rows"], "plugin_rows", "table", True, None)


def test_the_worker_waits_for_a_version_table_with_a_row():
    engine = create_engine("sqlite://")
    meta = MetaData()
    version = Table("alembic_version_x", meta, Column("version_num", String(32)))
    with engine.begin() as connection:
        assert not _stamped(connection, "alembic_version_x")
        meta.create_all(connection)
        assert not _stamped(connection, "alembic_version_x")
        connection.execute(version.insert().values(version_num="001"))
        assert _stamped(connection, "alembic_version_x")
        assert connection.execute(text("SELECT 1")).scalar() == 1


# ------------------------------------------------------------------ bootstrap


def test_bootstrap_builds_plugin_schemas_after_the_platforms_and_seeds_them_last(
    monkeypatch, caplog
):
    from app.db import bootstrap

    order: list[str] = []

    def broken_seed(engine):
        order.append("broken seed")
        raise RuntimeError("seed failed")

    first = Plugin(
        name="first",
        api_version=PLUGIN_API_VERSION,
        migrations="/first",
        on_bootstrap=lambda engine: order.append("first seed"),
    )
    second = Plugin(name="second", api_version=PLUGIN_API_VERSION, on_bootstrap=broken_seed)
    monkeypatch.setattr(bootstrap, "_wait_for_database", lambda engine: None)
    monkeypatch.setattr(bootstrap, "_platform_schema", lambda e, c: order.append("platform"))
    monkeypatch.setattr(bootstrap, "_seed", lambda engine: order.append("platform seed"))
    monkeypatch.setattr(plugins, "enabled", lambda: (first, second))
    monkeypatch.setattr(plugins, "upgrade", lambda p, url: order.append(f"{p.name} schema"))

    bootstrap.main()  # a failing plugin seed is reported, not fatal

    assert order == [
        "platform",
        "first schema",
        "second schema",
        "platform seed",
        "first seed",
        "broken seed",
    ]
    assert "plugin second: bootstrap failed" in caplog.text


# ------------------------------------------------------------------ routes, docs


def test_routes_are_mounted_and_their_tags_described():
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient

    router = APIRouter(prefix="/things", tags=["things"])

    @router.get("")
    def things():
        return ["a"]

    app = FastAPI(openapi_tags=[{"name": "core"}])
    plugins.mount(app, [Plugin(
        name="things", api_version=PLUGIN_API_VERSION, routers=(router,),
        openapi_tags=({"name": "things", "description": "Things a plugin keeps."},),
    )], "/api")

    client = TestClient(app)
    assert client.get("/api/things").json() == ["a"]
    tags = {t["name"]: t.get("description") for t in client.get("/openapi.json").json()["tags"]}
    assert tags == {"core": None, "things": "Things a plugin keeps."}


# ------------------------------------------------------------------ submissions


def _run_on_a_machine():
    from app.db.models import Campaign, Candidate, Machine, MachineState, Run, User

    supervisor, factory = _supervisor()
    with factory() as session:
        session.add(User(id=1, username="alice", password_hash="x"))
        session.add(Machine(id=1, name="node-1", host="h", gpu_count=8, gpu_type="H100",
                            state=MachineState.AVAILABLE.value))
        session.add(Campaign(id=1, owner_id=1, name="c", engine="sglang", image="i",
                             model_path="/m", served_model_name="m", search_space={}))
        session.add(Candidate(id=1, campaign_id=1, config={"tp": 2}, config_hash="h"))
        session.add(Run(id=1, campaign_id=1, candidate_id=1, machine_id=1, status="benching"))
        session.commit()
    return supervisor, factory


def test_a_plugin_adds_to_what_a_submission_carries(monkeypatch):
    from app.db.models import Run

    supervisor, factory = _run_on_a_machine()
    seen = {}

    def extras(session, run, context):
        seen.update(context)
        return {"contributor": "Team A", "board_token": "t0k"}

    monkeypatch.setattr(plugins, "enabled", lambda: (
        Plugin(name="boards", api_version=PLUGIN_API_VERSION, submission_extras=extras),
    ))
    with factory() as session:
        context = supervisor._bench_context(session.get(Run, 1))

    assert seen["hardware"]["card_type"] == "H100", "the plugin sees what will be sent"
    assert context["contributor"] == "Team A"
    assert context["extra_body"] == {"board_token": "t0k"}


def test_a_failing_plugin_leaves_the_submission_as_it_was(monkeypatch, caplog):
    from app.db.models import Run

    supervisor, factory = _run_on_a_machine()

    def broken(session, run, context):
        raise RuntimeError("boom")

    monkeypatch.setattr(plugins, "enabled", lambda: (
        Plugin(name="boards", api_version=PLUGIN_API_VERSION, submission_extras=broken),
    ))
    with factory() as session:
        context = supervisor._bench_context(session.get(Run, 1))

    assert context["contributor"] == "autotune:alice" and "extra_body" not in context
    assert "plugin boards: submission extras failed" in caplog.text


def test_extra_fields_reach_the_body_but_never_replace_the_endpoint():
    import json

    import httpx

    from app.evaluation import LLMBenchClient

    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(202, json={"id": 1})

    client = LLMBenchClient(base_url="http://t", api_key="llmb_k",
                            transport=httpx.MockTransport(handler), sleep=lambda _s: None)
    client.submit("b", "http://engine", "m",
                  extra_body={"board_token": "t0k", "endpoint_url": "http://elsewhere"})

    assert seen[0]["board_token"] == "t0k"
    assert seen[0]["endpoint_url"] == "http://engine"


# ------------------------------------------------------------------ promotion


def test_a_plugin_names_the_winner_of_a_campaign_it_runs():
    from types import SimpleNamespace

    from app.control.promotion.winner import deploy_branch_of, origin_of
    from app.plugins import PromotionOrigin

    campaign = SimpleNamespace(id=3, name="entrant", deploy_branch="")
    told = PromotionOrigin(kind="study", page="/studies/9", description="study **X**",
                           deploy_branch="release/x", remember_branch=False)

    origin = origin_of(campaign, 812, told)
    assert origin.kind == "study" and origin.page == "/studies/9"
    assert origin.describe() == "study **X**" and origin.remember_branch is False
    assert deploy_branch_of(campaign, told) == "release/x"

    plain = origin_of(campaign, 812)
    assert plain.page == "/campaigns/3" and "campaign **entrant**" in plain.describe()
    assert deploy_branch_of(SimpleNamespace(deploy_branch="release/own"), told) == "release/own"
