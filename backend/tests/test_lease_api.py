"""The lease API over real HTTP.

This is the one surface another team integrates against, so it is tested
through the whole stack — request parsing included. The bug that prompted these
tests lived nowhere else: `gpu_count: int = 8` on the request model meant a
re-lease with a minimal body silently resized a machine, and no test below the
HTTP layer could have seen it, because by the time the handler ran the default
and a real value were indistinguishable.
"""

from itertools import count

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core import apikeys
from app.core.auth import create_token
from app.db.base import Base, get_async_session
from app.db.models import ApiKey, LeaseState, Machine, MachineState, User
from app.main import app

_counter = count()


@pytest_asyncio.fixture
async def client(monkeypatch):
    """The app, wired to one in-memory database.

    Both engines point at the SAME sqlite instance via a shared cache: the
    lease endpoints write through the async session and read back through the
    sync one (which is how they reuse the supervisor's own predicates), so two
    separate databases would make every read look empty.

    The name is unique per test because a shared-cache database outlives the
    connection that created it — reusing one name leaked rows from the previous
    test into the next, which showed up as a duplicate-primary-key error on the
    fixture's own seed data.
    """
    name = f"lease_api_{next(_counter)}"
    db = f"sqlite+aiosqlite:///file:{name}?mode=memory&cache=shared&uri=true"
    sync_db = f"sqlite:///file:{name}?mode=memory&cache=shared&uri=true"

    engine = create_async_engine(db)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    sync_engine = create_engine(sync_db)
    sync_factory = sessionmaker(sync_engine, expire_on_commit=False)
    monkeypatch.setattr("app.api.leases.sync_session_factory", sync_factory)

    async def _session():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_async_session] = _session

    secret, prefix, key_hash = apikeys.mint()
    async with factory() as session:
        session.add(User(id=1, username="admin", password_hash="x", role="admin"))
        session.add(ApiKey(name="fleet", prefix=prefix, key_hash=key_hash, owner_id=1))
        await session.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
        http.headers["X-API-Key"] = secret
        http.db = factory  # the tests read rows back directly
        yield http

    app.dependency_overrides.clear()
    await engine.dispose()
    sync_engine.dispose()


async def _lease(client, **body):
    return await client.post("/api/machines/lease", json={"name": "node-24", **body})


# -- registering and re-leasing -----------------------------------------------


async def test_a_new_machine_is_registered_and_leased(client):
    response = await _lease(client, host="10.0.0.1", gpu_count=4, gpu_type="H100")

    assert response.status_code == 200
    body = response.json()
    assert body["machine"] == "node-24"
    assert body["lease_state"] == LeaseState.ACTIVE.value
    assert body["lease_holder"] == "key:fleet", "attributed to the key, not to a person"
    assert body["gpu_count"] == 4
    assert body["state"] == MachineState.AVAILABLE.value


async def test_a_new_machine_without_a_host_is_refused(client):
    response = await _lease(client, gpu_count=4)
    assert response.status_code == 422
    assert "host" in response.json()["detail"]


async def test_re_leasing_with_a_minimal_body_keeps_the_machine_as_it_was(client):
    """The idempotency promise, and the bug it hid: `{"name": …}` used to reset
    an explicitly-registered 4-card box to the schema's default of 8."""
    await _lease(client, host="10.0.0.1", gpu_count=4, gpu_type="H100", ssh_port=2222)

    again = await _lease(client)

    assert again.status_code == 200
    body = again.json()
    assert body["gpu_count"] == 4, "an omitted field means 'leave it', not 'reset it'"
    assert body["host"] == "10.0.0.1"
    async with client.db() as session:
        machine = await session.get(Machine, 1)
        assert machine.ssh_port == 2222
        assert machine.gpu_type == "H100"


async def test_re_leasing_still_applies_the_fields_that_were_sent(client):
    await _lease(client, host="10.0.0.1", gpu_count=4)

    body = (await _lease(client, gpu_count=8)).json()

    assert body["gpu_count"] == 8


async def test_leasing_is_idempotent_enough_to_retry(client):
    first = await _lease(client, host="10.0.0.1", gpu_count=4)
    second = await _lease(client, host="10.0.0.1", gpu_count=4)
    assert first.status_code == second.status_code == 200
    assert first.json()["machine"] == second.json()["machine"]


# -- authentication -----------------------------------------------------------


async def test_a_request_with_no_credential_is_rejected(client):
    response = await client.post(
        "/api/machines/lease", json={"name": "node-24", "host": "10.0.0.1"},
        headers={"X-API-Key": ""},
    )
    assert response.status_code == 401


async def test_a_garbage_key_is_rejected(client):
    response = await client.post(
        "/api/machines/lease", json={"name": "node-24", "host": "10.0.0.1"},
        headers={"X-API-Key": "atk_deadbeef_nope"},
    )
    assert response.status_code == 401


async def test_a_revoked_key_stops_working(client):
    await _lease(client, host="10.0.0.1")
    async with client.db() as session:
        from datetime import UTC, datetime

        key = await session.get(ApiKey, 1)
        key.revoked_at = datetime.now(UTC)
        await session.commit()

    assert (await _lease(client)).status_code == 401


async def test_a_browser_token_works_on_the_same_endpoints(client):
    """A person should be able to try any of this from /api/docs while logged
    in — otherwise the interactive reference is not usable as a reference."""
    async with client.db() as session:
        token = create_token(await session.get(User, 1))

    response = await client.post(
        "/api/machines/lease", json={"name": "node-24", "host": "10.0.0.1"},
        headers={"X-API-Key": "", "Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json()["lease_holder"] == "admin"


# -- ending a lease -----------------------------------------------------------


async def test_ending_a_lease_starts_a_drain_rather_than_finishing_immediately(client):
    await _lease(client, host="10.0.0.1")

    response = await client.post("/api/machines/node-24/lease/end", json={"mode": "polite"})

    assert response.status_code == 200
    assert response.json()["lease_state"] == LeaseState.DRAINING.value


async def test_an_unknown_mode_is_refused(client):
    await _lease(client, host="10.0.0.1")
    response = await client.post("/api/machines/node-24/lease/end", json={"mode": "brutal"})
    assert response.status_code == 422


async def test_ending_a_lease_that_does_not_exist_is_a_conflict(client):
    await _lease(client, host="10.0.0.1")
    await client.post("/api/machines/node-24/lease/end", json={"mode": "polite"})
    async with client.db() as session:
        machine = await session.get(Machine, 1)
        machine.lease_state = LeaseState.RELEASED.value
        await session.commit()

    response = await client.post("/api/machines/node-24/lease/end", json={"mode": "polite"})
    assert response.status_code == 409


async def test_leasing_a_draining_machine_is_refused(client):
    """Otherwise the new lease inherits a teardown already in progress, and the
    machine is taken apart underneath whoever just asked for it."""
    await _lease(client, host="10.0.0.1")
    await client.post("/api/machines/node-24/lease/end", json={"mode": "polite"})

    response = await _lease(client)

    assert response.status_code == 409
    assert "handed back" in response.json()["detail"]


async def test_a_deadline_is_recorded_as_an_instant(client):
    await _lease(client, host="10.0.0.1")
    response = await client.post(
        "/api/machines/node-24/lease/end", json={"mode": "polite", "deadline_seconds": 1800}
    )
    assert response.json()["lease_deadline_at"] is not None


async def test_escalating_from_polite_to_eager_is_allowed(client):
    """How a caller changes their mind: polite first, eager when it turns out
    they needed the machine sooner."""
    await _lease(client, host="10.0.0.1")
    await client.post("/api/machines/node-24/lease/end", json={"mode": "polite"})

    response = await client.post("/api/machines/node-24/lease/end", json={"mode": "eager"})

    assert response.status_code == 200
    assert response.json()["lease_end_mode"] == "eager"


# -- status -------------------------------------------------------------------


async def test_status_reports_the_word_an_external_caller_acts_on(client):
    await _lease(client, host="10.0.0.1")
    body = (await client.get("/api/machines/node-24/lease")).json()
    assert body["readiness"] in ("busy", "idle", "returnable")
    assert "headline" in body["stage"]


async def test_status_for_an_unknown_machine_is_a_404(client):
    assert (await client.get("/api/machines/nope/lease")).status_code == 404


async def test_the_machine_listing_carries_the_lease(client):
    """`/machines` is what the Resources page renders from. The lease columns
    were added to the table and to the lease endpoints but not to this
    response model, so the page saw `undefined` and offered to lease a machine
    it was already holding — a schema omission no lease-endpoint test could
    catch.

    Fetched with a browser token: `/machines` is the UI's own endpoint and
    takes a JWT only. An external caller reads `/machines/lease` instead."""
    await _lease(client, host="10.0.0.1")
    async with client.db() as session:
        token = create_token(await session.get(User, 1))

    body = (
        await client.get(
            "/api/machines", headers={"X-API-Key": "", "Authorization": f"Bearer {token}"}
        )
    ).json()

    assert body[0]["lease_state"] == LeaseState.ACTIVE.value
    assert body[0]["lease_holder"] == "key:fleet"
    # The field is exposed in the schema (the regression this guards). It is now
    # null by default: a lease has no expiry unless one is asked for.
    assert "lease_due_at" in body[0]
    assert body[0]["lease_due_at"] is None


async def test_the_fleet_listing_covers_every_machine(client):
    await _lease(client, host="10.0.0.1")
    await client.post("/api/machines/lease", json={"name": "node-25", "host": "10.0.0.2"})

    body = (await client.get("/api/machines/lease")).json()

    assert {m["machine"] for m in body} == {"node-24", "node-25"}


@pytest.mark.parametrize("path", ["/api/machines/lease", "/api/machines/node-24/lease"])
async def test_status_endpoints_need_a_credential(client, path):
    assert (await client.get(path, headers={"X-API-Key": ""})).status_code == 401
