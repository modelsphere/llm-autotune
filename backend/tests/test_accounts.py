"""Account management: changing your own password, and who is an admin.

The rule worth testing is the one that has no undo. A platform with no admin
cannot lease a machine, mint a key, or promote anybody — and the only way back
is editing the database by hand.
"""

import itertools

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base, get_async_session
from app.main import app

_DB = itertools.count()


@pytest.fixture
async def client():
    engine = create_async_engine(f"sqlite+aiosqlite:///file:acct{next(_DB)}?mode=memory&cache=shared&uri=true")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_async_session] = override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t/api") as http:
        yield http
    app.dependency_overrides.clear()
    await engine.dispose()


async def _register(http, username, password="secret123"):
    r = await http.post("/auth/register", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# -- changing your own password ----------------------------------------------


async def test_a_password_change_needs_the_current_one(client):
    """Being logged in is not enough. A token left open on a shared machine
    must not be usable to lock its owner out of their own account."""
    head = await _register(client, "alice")
    r = await client.post(
        "/auth/password",
        json={"current_password": "wrong", "new_password": "newsecret1"},
        headers=head,
    )
    assert r.status_code == 403


async def test_the_new_password_is_what_logs_you_in_afterwards(client):
    head = await _register(client, "bob")
    r = await client.post(
        "/auth/password",
        json={"current_password": "secret123", "new_password": "newsecret1"},
        headers=head,
    )
    assert r.status_code == 200
    assert r.json()["access_token"], "hand back a working token, not nothing"

    old = await client.post("/auth/login", json={"username": "bob", "password": "secret123"})
    assert old.status_code == 401
    new = await client.post("/auth/login", json={"username": "bob", "password": "newsecret1"})
    assert new.status_code == 200


async def test_setting_the_same_password_again_is_refused(client):
    head = await _register(client, "carol")
    r = await client.post(
        "/auth/password",
        json={"current_password": "secret123", "new_password": "secret123"},
        headers=head,
    )
    assert r.status_code == 422


async def test_a_short_password_is_refused_before_it_is_stored(client):
    head = await _register(client, "dan")
    r = await client.post(
        "/auth/password",
        json={"current_password": "secret123", "new_password": "abc"},
        headers=head,
    )
    assert r.status_code == 422
    ok = await client.post("/auth/login", json={"username": "dan", "password": "secret123"})
    assert ok.status_code == 200, "the old password still works"


# -- who is an admin ---------------------------------------------------------


async def test_the_first_user_is_admin_and_can_list_everyone(client):
    admin = await _register(client, "root")
    await _register(client, "someone")
    r = await client.get("/auth/users", headers=admin)
    assert r.status_code == 200
    assert [u["username"] for u in r.json()] == ["root", "someone"]
    assert [u["role"] for u in r.json()] == ["admin", "user"]


async def test_an_ordinary_user_cannot_list_or_promote(client):
    await _register(client, "root")
    plain = await _register(client, "nobody")
    assert (await client.get("/auth/users", headers=plain)).status_code == 403
    r = await client.put("/auth/users/1/role", json={"role": "user"}, headers=plain)
    assert r.status_code == 403


async def test_the_last_admin_cannot_be_demoted(client):
    """Including by themselves. Nothing in the UI can undo it: leasing a
    machine, minting a key and promoting a user are all admin-gated."""
    admin = await _register(client, "root")
    await _register(client, "someone")
    r = await client.put("/auth/users/1/role", json={"role": "user"}, headers=admin)
    assert r.status_code == 409
    assert "only admin" in r.json()["detail"]


async def test_an_admin_may_step_down_once_there_is_another(client):
    admin = await _register(client, "root")
    await _register(client, "deputy")
    assert (
        await client.put("/auth/users/2/role", json={"role": "admin"}, headers=admin)
    ).status_code == 200
    stepped = await client.put("/auth/users/1/role", json={"role": "user"}, headers=admin)
    assert stepped.status_code == 200
    assert stepped.json()["role"] == "user"


async def test_an_unknown_role_is_refused(client):
    admin = await _register(client, "root")
    await _register(client, "someone")
    r = await client.put("/auth/users/2/role", json={"role": "superuser"}, headers=admin)
    assert r.status_code == 422
