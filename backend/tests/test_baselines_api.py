"""The baselines CRUD surface: a production reference is a first-class thing,
keyed by (served model, engine, card type), managed by hand or from capture.
"""

from itertools import count

import httpx
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.auth import create_token
from app.db.base import Base, get_async_session
from app.db.models import User
from app.main import app

_counter = count()


@pytest_asyncio.fixture
async def client():
    name = f"bl_{next(_counter)}"
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
        await session.commit()
        token = create_token(await session.get(User, 1))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
        http.headers["Authorization"] = f"Bearer {token}"
        yield http

    app.dependency_overrides.clear()
    await engine.dispose()


async def test_create_derives_cards_and_lists_back(client):
    body = {
        "served_model_name": "glm-5", "engine": "sglang", "card_type": "A100",
        "engine_args": {"tp": 2, "chunked_prefill_size": 32768},
        "notes": "prod as of today",
    }
    created = (await client.post("/api/baselines", json=body)).json()
    assert created["cards"] == 2, "derived from tp the way a candidate's are"
    assert created["source"] == "manual"
    assert created["engine_args"]["chunked_prefill_size"] == 32768

    listed = (await client.get("/api/baselines")).json()
    assert [b["served_model_name"] for b in listed] == ["glm-5"]


async def test_the_identity_is_model_engine_and_card_type(client):
    base = {"served_model_name": "glm-5", "engine": "sglang", "card_type": "A100",
            "engine_args": {"tp": 2}}
    assert (await client.post("/api/baselines", json=base)).status_code == 200
    # Same model+engine on different hardware is a DIFFERENT baseline.
    assert (await client.post(
        "/api/baselines", json={**base, "card_type": "H100"}
    )).status_code == 200
    # A different engine, too.
    assert (await client.post(
        "/api/baselines", json={**base, "engine": "vllm"}
    )).status_code == 200
    # But the same triple collides.
    dup = await client.post("/api/baselines", json=base)
    assert dup.status_code == 409


async def test_a_pasted_command_is_parsed_into_engine_args(client):
    command = (
        '["python", "-m", "sglang.launch_server", "--served-model-name", "glm-5", '
        '"--tp", "4", "--enable-dp-attention", "--port", "8050"]'
    )
    # The preview endpoint parses without writing.
    preview = (await client.post("/api/baselines/parse", json={
        "served_model_name": "glm-5", "command": command,
    })).json()
    assert preview["engine_args"] == {"tp": "4", "enable_dp_attention": True}
    assert preview["cards"] == 4, "dp-attention does not multiply cards"
    assert "port" not in preview["engine_args"], "placement is stripped"

    # And create accepts a command directly, storing the parsed args.
    created = (await client.post("/api/baselines", json={
        "served_model_name": "glm-5", "engine": "sglang", "card_type": "A100",
        "command": command,
    })).json()
    assert created["engine_args"] == {"tp": "4", "enable_dp_attention": True}


async def test_update_and_delete(client):
    created = (await client.post("/api/baselines", json={
        "served_model_name": "glm-5", "engine": "sglang", "card_type": "A100",
        "engine_args": {"tp": 2},
    })).json()
    bid = created["id"]

    updated = (await client.put(f"/api/baselines/{bid}", json={
        "served_model_name": "glm-5", "engine": "sglang", "card_type": "A100",
        "engine_args": {"tp": 4}, "notes": "bumped",
    })).json()
    assert updated["engine_args"]["tp"] == 4 and updated["notes"] == "bumped"

    assert (await client.delete(f"/api/baselines/{bid}")).status_code == 204
    assert (await client.get("/api/baselines")).json() == []
