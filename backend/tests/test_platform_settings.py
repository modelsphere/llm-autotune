"""The admin Settings page: external platforms' addresses over the install's
env, LLM endpoints registered once and used by report writing and agent
policies, and publishing an export to a catalog server's write API."""

import json
from itertools import count

import httpx
import pytest
import pytest_asyncio
import yaml
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import catalog_export as catalog_api
from app.api import settings as settings_api
from app.control.orchestrator.policy_session import llm_endpoint_env, redact_secrets
from app.core import platform_settings as ps
from app.core.auth import create_token
from app.core.config import get_settings
from app.db.base import Base, get_async_session
from app.db.models import Campaign, Candidate, LlmEndpoint, Policy, Result, Run, User
from app.main import app

_counter = count()
HF = "Qwen/Qwen3.5-35B-A3B"
CATALOG = "http://catalog.example:31556"
TOKEN = "catalog-write-key-123"
LLM_KEY = "sk-llm-secret-456"


@pytest_asyncio.fixture
async def stack():
    name = f"ps_{next(_counter)}"
    db = f"sqlite+aiosqlite:///file:{name}?mode=memory&cache=shared&uri=true"
    engine = create_async_engine(db)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _session():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_async_session] = _session
    async with factory() as session:
        session.add(User(id=1, username="admin", password_hash="x", role="admin"))
        session.add(User(id=2, username="alice", password_hash="x", role="user"))
        await session.commit()
        admin = create_token(await session.get(User, 1))
        user = create_token(await session.get(User, 2))
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://t") as http,
        httpx.AsyncClient(transport=transport, base_url="http://t") as plain,
    ):
        http.headers["Authorization"] = f"Bearer {admin}"
        plain.headers["Authorization"] = f"Bearer {user}"
        yield http, plain, factory
    app.dependency_overrides.clear()
    await engine.dispose()


# -- external platforms --------------------------------------------------------------


async def test_settings_fall_back_to_the_install_and_never_return_the_key(stack, monkeypatch):
    http, plain, factory = stack
    monkeypatch.setattr(get_settings(), "llmbench_victorialogs_url", "http://vl.logging:9428/")
    out = (await http.get("/api/settings")).json()
    assert out["victorialogs_url"] == "" and out["victorialogs_install"] == "http://vl.logging:9428"
    assert out["catalog_index_url"] == get_settings().catalog_index_url
    assert out["catalog_writable"] is False
    assert (await plain.get("/api/settings")).status_code == 403, "admins only"

    res = await http.put(
        "/api/settings",
        json={"victorialogs_url": "http://vl.other:9428/", "catalog_url": f"{CATALOG}/",
              "catalog_token": TOKEN},
    )
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["victorialogs_url"] == "http://vl.other:9428"
    assert out["catalog_url"] == CATALOG
    assert out["catalog_index_url"] == f"{CATALOG}/index-v2.json"
    assert out["catalog_has_token"] and out["catalog_writable"]
    assert TOKEN not in res.text
    async with factory() as s:
        resolved = await ps.resolve(s)
        assert resolved.catalog_token == TOKEN and resolved.victorialogs_source == "settings"

    # An absent token is kept; "" clears a URL back to the install's value.
    out = (await http.put("/api/settings", json={"victorialogs_url": ""})).json()
    assert out["victorialogs_url"] == "" and out["catalog_has_token"]
    async with factory() as s:
        assert (await ps.resolve(s)).victorialogs_url == "http://vl.logging:9428"
    out = (await http.put("/api/settings", json={"catalog_token": ""})).json()
    assert not out["catalog_has_token"] and not out["catalog_writable"]
    bad = await http.put("/api/settings", json={"catalog_url": "catalog.example"})
    assert bad.status_code == 422


async def test_the_dataset_form_is_prefilled_from_settings(stack, monkeypatch):
    http, _, factory = stack
    from app.api import datasets

    monkeypatch.setattr(get_settings(), "llmbench_victorialogs_url", "http://vl.install:9428")
    async with factory() as s:
        assert await datasets._log_store(s) == "http://vl.install:9428"
        assert await datasets._log_store(s, "http://typed:9428/") == "http://typed:9428"
    await http.put("/api/settings", json={"victorialogs_url": "http://vl.ui:9428"})
    async with factory() as s:
        assert await datasets._log_store(s) == "http://vl.ui:9428"


def _catalog_server(calls: list, *, writable=True, token=TOKEN, models=None):
    """A catalog-server's reads and api/v2 writes, recording each request."""
    models = models if models is not None else {}

    def handler(request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        calls.append((method, path, dict(request.url.params), dict(request.headers)))
        if path == "/index-v2.json":
            return httpx.Response(200, json={"apiVersion": "catalog.swiss/v2", "models": [
                {"name": m, "variants": []} for m in models]})
        if path == "/api/v2":
            return httpx.Response(200, json={"apiVersion": "catalog.swiss/v2", "write": writable})
        if method in ("PUT", "POST", "DELETE"):
            if request.headers.get("Authorization") != f"Bearer {token}":
                return httpx.Response(401, json={"error": "send Authorization: Bearer <token>"})
        parts = path.split("/")
        if path.endswith("/metadata") and method == "GET":
            model = parts[4]
            if model not in models:
                return httpx.Response(404, text="not in this catalog\n")
            return httpx.Response(200, text="apiVersion: catalog.swiss/v2\n",
                                  headers={"ETag": f'"sha256:{model}-meta"'})
        if "/variants/" in path and method == "GET":
            model, vid = parts[4], parts[6]
            if vid not in models.get(model, {}):
                return httpx.Response(404, text="not in this catalog\n")
            return httpx.Response(200, text="", headers={"ETag": f'"sha256:{vid}"'})
        if path == "/api/v2/models" and method == "POST":
            body = json.loads(request.content)
            name = yaml.safe_load(body["metadata"])["name"]
            commit = None if request.url.params.get("dryRun") else "c" * 40
            return httpx.Response(201, json={
                "commit": commit, "path": f"models/{name}/metadata.yaml",
                "variants": [{"id": "x"}]})
        if method == "PUT" and "/variants/" in path:
            model, vid = parts[4], parts[6]
            exists = vid in models.get(model, {})
            if exists and not request.headers.get("If-Match"):
                return httpx.Response(428, json={"error": "If-Match"})
            if not exists and request.headers.get("If-None-Match") != "*":
                return httpx.Response(412, json={"error": "exists"})
            doc = yaml.safe_load(request.content)
            dry = bool(request.url.params.get("dryRun"))
            if not dry:
                models.setdefault(model, {})[vid] = {}
            return httpx.Response(201 if not exists else 200, json={
                "commit": None if dry else "d" * 40, "path": f"models/{model}/{vid}.yaml",
                "version": doc.get("version"), "digest": "sha256:" + "e" * 64,
                **({"dryRun": True} if dry else {})})
        if method == "PUT" and path.endswith("/metadata"):
            if request.headers.get("If-Match") != f'"sha256:{parts[4]}-meta"':
                return httpx.Response(412, json={"error": "changed"})
            dry = bool(request.url.params.get("dryRun"))
            # As catalog-server does: defaultVariant must be a variant the
            # model has now (a dry run's variant writes committed nothing).
            default = (yaml.safe_load(request.content) or {}).get("defaultVariant")
            if default is not None and default not in models.get(parts[4], {}):
                return httpx.Response(422, json={
                    "error": "not a change this catalog takes",
                    "problems": [f"models/{parts[4]}/metadata.yaml: defaultVariant: "
                                 f"{default!r} is not a variant of {parts[4]}"]})
            return httpx.Response(200, json={"commit": None if dry else "f" * 40,
                                             "path": f"models/{parts[4]}/metadata.yaml"})
        return httpx.Response(404, text="not in this catalog\n")

    return httpx.MockTransport(handler)


async def test_the_catalog_check_tells_a_bad_key_from_a_good_one(stack, monkeypatch):
    http, _, _ = stack
    calls: list = []
    monkeypatch.setattr(settings_api, "_transport", lambda: _catalog_server(calls))
    await http.put("/api/settings", json={"catalog_url": CATALOG, "catalog_token": TOKEN})
    out = (await http.post("/api/settings/check", json={"target": "catalog"})).json()
    assert out["ok"] and out["writable"] and out["token_ok"], out
    probe = next(c for c in calls if c[0] == "PUT")
    assert probe[2] == {"dryRun": "true"}, "the key check commits nothing"
    out = (await http.post("/api/settings/check",
                           json={"target": "catalog", "token": "wrong"})).json()
    assert not out["ok"] and out["token_ok"] is False and "refused" in out["message"]


# -- LLM endpoints ---------------------------------------------------------------------


async def test_an_endpoint_is_registered_once_and_its_key_never_comes_back(stack, monkeypatch):
    http, plain, factory = stack
    body = {"name": "glm", "base_url": "https://llm.example/v1/", "model": "glm-5",
            "api_key": LLM_KEY}
    assert (await plain.post("/api/llm-endpoints", json=body)).status_code == 403
    res = await http.post("/api/llm-endpoints", json=body)
    assert res.status_code == 201, res.text
    endpoint = res.json()
    assert endpoint["base_url"] == "https://llm.example/v1" and endpoint["has_api_key"]
    assert LLM_KEY not in res.text
    assert (await http.post("/api/llm-endpoints", json=body)).status_code == 409
    listed = await plain.get("/api/llm-endpoints")
    assert listed.status_code == 200 and LLM_KEY not in listed.text, "anyone may choose one"

    # A key left out is kept.
    changed = await http.put(f"/api/llm-endpoints/{endpoint['id']}",
                             json={**body, "model": "glm-5.1", "api_key": None})
    assert changed.json()["model"] == "glm-5.1" and changed.json()["has_api_key"]

    # Report writing uses it; deleting it is refused while it does.
    out = (await http.put("/api/settings", json={"reports_llm_endpoint_id": endpoint["id"]})).json()
    assert out["reports_llm_endpoint_id"] == endpoint["id"]
    async with factory() as s:
        target = await ps.reports_llm(s)
        assert target is not None and target.api_key == LLM_KEY and target.model == "glm-5.1"
    refused = await http.delete(f"/api/llm-endpoints/{endpoint['id']}")
    assert refused.status_code == 409 and "reports" in refused.text
    await http.put("/api/settings", json={"clear_reports_llm_endpoint": True})
    assert (await http.delete(f"/api/llm-endpoints/{endpoint['id']}")).status_code == 204


async def test_the_test_call_sends_the_key_and_reports_the_answer(stack, monkeypatch):
    http, _, _ = stack
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["model"] = json.loads(request.content)["model"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "ready"}}]})

    monkeypatch.setattr(settings_api, "_transport", lambda: httpx.MockTransport(handler))
    endpoint = (await http.post("/api/llm-endpoints", json={
        "name": "e", "base_url": "https://llm.example/v1", "model": "m", "api_key": LLM_KEY,
    })).json()
    out = (await http.post(f"/api/llm-endpoints/{endpoint['id']}/test")).json()
    assert out["ok"] and out["reply"] == "ready", out
    assert seen == {"url": "https://llm.example/v1/chat/completions",
                    "auth": f"Bearer {LLM_KEY}", "model": "m"}

    monkeypatch.setattr(settings_api, "_transport", lambda: httpx.MockTransport(
        lambda r: httpx.Response(401, text=f"bad key {LLM_KEY}")))
    out = (await http.post(f"/api/llm-endpoints/{endpoint['id']}/test")).json()
    assert not out["ok"] and "401" in out["message"] and LLM_KEY not in out["message"]


async def test_a_policy_names_an_endpoint_and_its_container_gets_it(stack):
    http, _, factory = stack
    endpoint = (await http.post("/api/llm-endpoints", json={
        "name": "agent", "base_url": "https://llm.example/v1/", "model": "glm-5",
        "api_key": LLM_KEY,
    })).json()
    policy = {"name": "llm-agent", "image": "agent:1", "llm_endpoint_id": endpoint["id"],
              "env": {"LLM_MODEL": "old-model", "OTHER": "1"}}
    res = await http.post("/api/policies", json=policy)
    assert res.status_code == 201, res.text
    assert res.json()["llm_endpoint_id"] == endpoint["id"]
    missing = await http.post("/api/policies", json={**policy, "name": "x", "llm_endpoint_id": 999})
    assert missing.status_code == 422

    async with factory() as s:
        row = await s.get(Policy, res.json()["id"])
        ep = await s.get(LlmEndpoint, endpoint["id"])

    class _Db:
        def get(self, model, key):
            return ep if model is LlmEndpoint and key == ep.id else None

    plain = llm_endpoint_env(_Db(), row, secret=False)
    secret = llm_endpoint_env(_Db(), row, secret=True)
    assert plain == {"LLM_BASE_URL": "https://llm.example/v1", "LLM_MODEL": "glm-5"}
    assert secret == {"LLM_API_KEY": LLM_KEY}
    env = {**row.env, **plain, **secret}
    assert env["LLM_MODEL"] == "glm-5" and env["OTHER"] == "1", "the endpoint wins over env"
    command = f"docker run -e LLM_API_KEY={LLM_KEY} agent:1"
    assert LLM_KEY not in redact_secrets(command, "", secret)

    row.llm_endpoint_id = None
    assert llm_endpoint_env(_Db(), row, secret=False) == {}


# -- publishing to the catalog ---------------------------------------------------------


async def _seed(factory):
    async with factory() as s:
        s.add(Campaign(
            id=9, owner_id=1, name="qwen3.5 h800", engine="sglang",
            image="registry.example.com/team/sglang:v0.5.15",
            model_path="/models/Qwen/Qwen3.5-35B-A3B", served_model_name="Qwen3.5-35B-A3B",
            search_space={"grid": {"mem_fraction_static": [0.83, 0.88]}},
            objective={"target_metric": "request_output_tps", "direction": "maximize"},
            benchmark_slug="bench-82",
        ))
        for run_id, mem, value in ((101, 0.83, 1000.0), (102, 0.88, 1200.0)):
            s.add(Candidate(id=run_id, campaign_id=9, config_hash=f"h{run_id}",
                            config={"tp": 2, "mem_fraction_static": mem}))
            s.add(Run(id=run_id, campaign_id=9, candidate_id=run_id,
                      env_snapshot={"card_type": "H800"}))
            s.add(Result(run_id=run_id, source="llmbench", objective_value=value, feasible=True,
                         metrics={"request_output_tps": value}))
        await s.commit()


PAIR = [{"run_id": 101, "role": "baseline"}, {"run_id": 102, "role": "optimized"}]


async def test_publish_needs_a_catalog_server_and_its_key(stack):
    http, _, factory = stack
    await _seed(factory)
    res = await http.post("/api/catalog-export/publish", json={"variants": PAIR, "hf": HF})
    assert res.status_code == 409 and "Settings" in res.text


async def test_a_new_model_is_checked_then_added_in_one_commit(stack, monkeypatch):
    http, _, factory = stack
    await _seed(factory)
    calls: list = []
    server = _catalog_server(calls)
    monkeypatch.setattr(catalog_api, "_publish_transport", lambda: server)
    await http.put("/api/settings", json={"catalog_url": CATALOG, "catalog_token": TOKEN})
    preview = (await http.post("/api/catalog-export/preview",
                               json={"variants": PAIR, "hf": HF, "use_catalog": False})).json()
    assert preview["catalog_writable"] and preview["catalog_url"] == CATALOG

    body = {"variants": PAIR, "hf": HF, "use_catalog": False, "message": "qwen3.5: tuned · H800"}
    dry = (await http.post("/api/catalog-export/publish", json=body)).json()
    assert dry["ok"] and dry["dry_run"], dry
    assert [w["action"] for w in dry["writes"]] == ["add_model"]
    assert all(c[2].get("dryRun") == "true" for c in calls if c[0] == "POST")
    # No report goes up (the server takes none), so no tuning entry either.
    exported = dry["export"]
    assert [f["kind"] for f in exported["files"]] == ["variant", "variant", "metadata"]
    assert exported["variants"][1]["tuning"] == "no_report"

    calls.clear()
    done = (await http.post("/api/catalog-export/publish",
                            json={**body, "dry_run": False})).json()
    assert done["ok"] and done["writes"][0]["commit"] == "c" * 40, done
    posts = [c for c in calls if c[0] == "POST"]
    assert [p[2] for p in posts] == [{"dryRun": "true"}, {}], "every write is dry-run first"
    assert posts[1][3]["x-commit-message"] == "qwen3.5: tuned  H800", "ASCII in a header"


async def test_an_existing_model_gets_variant_puts_then_its_metadata(stack, monkeypatch):
    http, _, factory = stack
    await _seed(factory)
    calls: list = []
    published = {"qwen3.5-35b-a3b": {"sglang-tp2-h800-baseline": {}}}
    monkeypatch.setattr(catalog_api, "_publish_transport",
                        lambda: _catalog_server(calls, models=published))
    await http.put("/api/settings", json={"catalog_url": CATALOG, "catalog_token": TOKEN})
    body = {"variants": PAIR, "hf": HF, "use_catalog": False, "dry_run": False}
    done = (await http.post("/api/catalog-export/publish", json=body)).json()
    assert done["ok"], done
    assert [(w["path"].rsplit("/", 1)[-1], w["action"]) for w in done["writes"]] == [
        ("sglang-tp2-h800-baseline.yaml", "replace"),
        ("sglang-tp2-h800-optimized.yaml", "add"),
        ("metadata.yaml", "metadata"),
    ]
    puts = [c for c in calls if c[0] == "PUT" and not c[2]]
    assert puts[0][3]["if-match"] == '"sha256:sglang-tp2-h800-baseline"'
    assert puts[1][3]["if-none-match"] == "*"
    assert puts[2][3]["if-match"] == '"sha256:qwen3.5-35b-a3b-meta"'


async def test_a_refused_write_stops_before_anything_is_committed(stack, monkeypatch):
    http, _, factory = stack
    await _seed(factory)
    calls: list = []
    monkeypatch.setattr(catalog_api, "_publish_transport",
                        lambda: _catalog_server(calls, token="another"))
    await http.put("/api/settings", json={"catalog_url": CATALOG, "catalog_token": TOKEN})
    body = {"variants": PAIR, "hf": HF, "use_catalog": False, "dry_run": False}
    out = (await http.post("/api/catalog-export/publish", json=body)).json()
    assert not out["ok"] and "401" in out["message"]
    assert all(c[2].get("dryRun") == "true" for c in calls if c[0] in ("POST", "PUT")), (
        "the real writes never started"
    )


@pytest.mark.parametrize("url", ["", f"{CATALOG}"])
async def test_the_export_reads_the_catalog_set_in_settings(stack, monkeypatch, url):
    http, _, factory = stack
    seen = []

    async def fake_lookup(index_url, name, hf, transport=None):
        seen.append(index_url)
        return None, ""

    monkeypatch.setattr(catalog_api, "lookup", fake_lookup)
    await _seed(factory)
    if url:
        await http.put("/api/settings", json={"catalog_url": url})
    await http.post("/api/catalog-export/preview", json={"variants": PAIR, "hf": HF})
    expected = f"{CATALOG}/index-v2.json" if url else get_settings().catalog_index_url
    assert seen == [expected]


async def test_a_new_default_variant_passes_the_dry_run_then_lands(stack, monkeypatch):
    """The catalog takes a defaultVariant only if the model has that variant. A
    dry run commits nothing, so a variant this publish adds is not there when
    the metadata is checked: the dry run checks the metadata without that key,
    and the real write sends it after the variant is committed."""
    http, _, factory = stack
    await _seed(factory)
    calls: list = []
    published = {"qwen3.5-35b-a3b": {"sglang-tp1-b300": {}}}
    monkeypatch.setattr(catalog_api, "_publish_transport",
                        lambda: _catalog_server(calls, models=published))
    await http.put("/api/settings", json={"catalog_url": CATALOG, "catalog_token": TOKEN})
    body = {"variants": [{"run_id": 102, "role": "optimized"}], "hf": HF, "use_catalog": False,
            "default_variant": "sglang-tp2-h800-optimized"}
    dry = (await http.post("/api/catalog-export/publish", json=body)).json()
    assert dry["ok"], dry
    meta = next(w for w in dry["writes"] if w["action"] == "metadata")
    assert "checked without defaultVariant" in meta["message"]
    done = (await http.post("/api/catalog-export/publish", json={**body, "dry_run": False})).json()
    assert done["ok"], done
    sent = [c for c in calls if c[0] == "PUT" and c[1].endswith("/metadata") and not c[2]]
    assert len(sent) == 1, "the real metadata write went out once, with its default"
