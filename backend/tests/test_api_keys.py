"""Who sees and revokes which API keys.

A key acts with its owner's authority, so another user revoking it is taking
away someone else's access, and listing it tells them what the owner wired
the platform into. A key the platform mints for a policy session is the
running search's own credential: revoking it by hand would cut the search off
mid-night, so it is not offered at all.
"""

import itertools

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base, get_async_session
from app.db.models import ApiKey
from app.main import app

_DB = itertools.count()


@pytest.fixture
async def env():
    engine = create_async_engine(
        f"sqlite+aiosqlite:///file:keys{next(_DB)}?mode=memory&cache=shared&uri=true"
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_async_session] = override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t/api") as http:
        yield http, factory
    app.dependency_overrides.clear()
    await engine.dispose()


async def _register(http, username):
    r = await http.post("/auth/register", json={"username": username, "password": "secret123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _mint(http, head, name):
    r = await http.post("/api-keys", json={"name": name}, headers=head)
    assert r.status_code == 201, r.text
    return r.json()


async def test_a_user_sees_only_their_own_keys(env):
    http, _ = env
    admin = await _register(http, "admin")  # the first user is the admin
    alice = await _register(http, "alice")
    await _mint(http, admin, "fleet-manager")
    await _mint(http, alice, "alice-script")

    mine = (await http.get("/api-keys", headers=alice)).json()
    assert [k["name"] for k in mine] == ["alice-script"]
    assert mine[0]["owner"] == "alice"

    everyone = (await http.get("/api-keys", headers=admin)).json()
    assert {(k["name"], k["owner"]) for k in everyone} == {
        ("fleet-manager", "admin"), ("alice-script", "alice"),
    }


async def test_a_user_cannot_revoke_someone_elses_key(env):
    http, _ = env
    admin = await _register(http, "admin")
    alice = await _register(http, "alice")
    theirs = await _mint(http, admin, "fleet-manager")

    r = await http.delete(f"/api-keys/{theirs['id']}", headers=alice)
    assert r.status_code == 404
    still = (await http.get("/api-keys", headers=admin)).json()
    assert still[0]["revoked_at"] is None

    mine = await _mint(http, alice, "alice-script")
    assert (await http.delete(f"/api-keys/{mine['id']}", headers=alice)).status_code == 200
    # An admin may revoke anyone's.
    assert (await http.delete(f"/api-keys/{theirs['id']}", headers=admin)).status_code == 200


async def test_policy_session_keys_are_not_listed_or_revocable(env):
    http, factory = env
    admin = await _register(http, "admin")
    async with factory() as session:
        key = ApiKey(name="policy-session-7", prefix="atk_sess0001", key_hash="x" * 64,
                     owner_id=1, policy_session_id=None)
        session.add(key)
        await session.flush()
        # Point it at a session without needing the session row: the FK is not
        # enforced by SQLite here, and the listing only reads the column.
        key.policy_session_id = 7
        await session.commit()
        key_id = key.id

    listed = (await http.get("/api-keys", headers=admin)).json()
    assert all(k["name"] != "policy-session-7" for k in listed)
    assert (await http.delete(f"/api-keys/{key_id}", headers=admin)).status_code == 404


async def test_a_blank_name_is_refused(env):
    http, _ = env
    head = await _register(http, "admin")
    r = await http.post("/api-keys", json={"name": "   "}, headers=head)
    assert r.status_code == 422


async def test_a_key_answers_who_am_i_and_stops_once_revoked(env):
    """The API keys page tells people to check a key with GET /api/auth/me."""
    http, _ = env
    await _register(http, "admin")
    alice = await _register(http, "alice")
    key = await _mint(http, alice, "ci")
    r = await http.get("/auth/me", headers={"X-API-Key": key["secret"]})
    assert r.status_code == 200 and r.json()["username"] == "alice"
    await http.delete(f"/api-keys/{key['id']}", headers=alice)
    r = await http.get("/auth/me", headers={"X-API-Key": key["secret"]})
    assert r.status_code == 401
