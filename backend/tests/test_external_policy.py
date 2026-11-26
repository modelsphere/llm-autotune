"""An external policy: one the platform does not run.

No container is started for its session. The campaign's owner asks for the
session's token and the policy speaks the same policy API from wherever it
runs; its heartbeat is the session's one sign of life.
"""
# ruff: noqa: F811  (the `night` fixture is imported, then named as a parameter)

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.control.orchestrator.lifecycle import as_utc
from app.core.auth import create_token
from app.db.models import (
    ApiKey,
    Campaign,
    Event,
    Policy,
    PolicySession,
    PolicySessionStatus,
    User,
)
from app.schemas.policy import PolicyIn
from tests.test_policy_sessions import (  # noqa: F401  (fixture)
    _serving_launch,
    _session_row,
    night,
)


class ExternalPolicy:
    """The policy's side of the policy API, holding the token it was issued."""

    def __init__(self, http, token: str):
        self.http = http
        self.token = token

    async def get(self, path):
        return await self.http.get(f"/api/policy/v1{path}", headers={"X-API-Key": self.token})

    async def post(self, path, json=None, **headers):
        return await self.http.post(
            f"/api/policy/v1{path}", json=json, headers={"X-API-Key": self.token, **headers}
        )

    async def delete(self, path):
        return await self.http.delete(
            f"/api/policy/v1{path}", headers={"X-API-Key": self.token}
        )

    async def heartbeat(self):
        response = await self.post(
            "/session/heartbeat",
            json={"phase": "searching", "sdk_version": "0.0-test", "contract_version": "1.8"},
        )
        assert response.status_code == 200, response.text
        return response.json()


async def _make_external(http) -> None:
    async with http.db() as session:
        policy = await session.get(Policy, 1)
        policy.external = True
        policy.image = ""
        policy.gpus_in_container = False
        policy.needs_model = False
        await session.commit()


async def _ask(http, user_id: int = 1):
    async with http.db() as session:
        bearer = create_token(await session.get(User, user_id))
    return await http.post(
        "/api/campaigns/1/session-token", headers={"Authorization": f"Bearer {bearer}"}
    )


async def _open(supervisor, http) -> ExternalPolicy:
    """tick → a machine is reserved; tick → the session is open; the owner
    asks for the token; first heartbeat; tick → SEARCHING."""
    supervisor.tick()
    supervisor.tick()
    issued = await _ask(http)
    assert issued.status_code == 200, issued.text
    policy = ExternalPolicy(http, issued.json()["token"])
    assert (await policy.heartbeat())["command"] == "run"
    supervisor.tick()
    assert (await _session_row(http)).status == PolicySessionStatus.SEARCHING.value
    return policy


async def test_a_session_opens_with_no_container(night):
    supervisor, driver, http = night
    await _make_external(http)

    supervisor.tick()
    supervisor.tick()

    row = await _session_row(http)
    assert row.status == PolicySessionStatus.STARTING.value
    assert row.machine_id == 1 and row.gpu_indices and row.ports
    assert row.container_name == ""
    assert not driver.workloads, "nothing is launched for an external policy"
    async with http.db() as session:
        assert not (await session.execute(select(ApiKey))).scalars().all(), (
            "no token exists until the owner asks for one"
        )
        started = (
            await session.execute(select(Event).where(Event.kind == "policy_session_started"))
        ).scalars().one()
        assert started.payload["external"] is True

    # With no container to be found gone, the session waits for its policy.
    for _ in range(3):
        supervisor.tick()
    assert (await _session_row(http)).status == PolicySessionStatus.STARTING.value


async def test_it_needs_no_address_for_a_container_to_call_home(night, monkeypatch):
    supervisor, _driver, http = night
    await _make_external(http)
    monkeypatch.setattr(supervisor.settings, "public_api_url", "")
    monkeypatch.setattr(supervisor.settings, "internal_api_url", "")

    supervisor.tick()
    supervisor.tick()

    assert (await _session_row(http)).status == PolicySessionStatus.STARTING.value


async def test_the_owner_is_issued_the_token_and_the_policy_works_with_it(night):
    supervisor, driver, http = night
    await _make_external(http)
    policy = await _open(supervisor, http)

    manifest = await policy.get("/session")
    assert manifest.status_code == 200, manifest.text

    launch = await _serving_launch(supervisor, policy)
    bench = await policy.post(f"/launches/{launch}/benchmarks", json={"suite": "screen"})
    assert bench.status_code == 201, bench.text
    for _ in range(4):
        supervisor.tick()
    measured = await policy.get(f"/benchmarks/{bench.json()['id']}")
    assert measured.json()["status"] == "succeeded", measured.text

    released = await policy.delete(f"/launches/{launch}")
    assert released.status_code == 200, released.text
    assert not driver.workloads, "still no policy container"


async def test_the_token_ends_with_the_window_and_follows_it(night):
    supervisor, _driver, http = night
    await _make_external(http)
    end = datetime.now(UTC) + timedelta(hours=60)
    async with http.db() as session:
        campaign = await session.get(Campaign, 1)
        campaign.window_end = end
        await session.commit()
    supervisor.tick()
    supervisor.tick()

    issued = (await _ask(http)).json()
    assert as_utc(datetime.fromisoformat(issued["expires_at"])) == end + timedelta(hours=1)
    assert issued["api_url"] == "http://platform.test"

    later = end + timedelta(hours=10)
    async with http.db() as session:
        campaign = await session.get(Campaign, 1)
        campaign.window_end = later
        await session.commit()
    supervisor.tick()
    async with http.db() as session:
        key = (await session.execute(select(ApiKey))).scalars().one()
        assert as_utc(key.expires_at) == later + timedelta(hours=1)


async def test_asking_again_issues_a_new_token_and_ends_the_old_one(night):
    supervisor, _driver, http = night
    await _make_external(http)
    first = await _open(supervisor, http)

    again = await _ask(http)
    assert again.status_code == 200, again.text
    second = ExternalPolicy(http, again.json()["token"])
    assert second.token != first.token

    assert (await second.get("/session")).status_code == 200
    assert (await first.get("/session")).status_code == 401

    # The window moving must not bring the ended token back.
    async with http.db() as session:
        campaign = await session.get(Campaign, 1)
        campaign.window_end = datetime.now(UTC) + timedelta(hours=30)
        await session.commit()
    supervisor.tick()
    assert (await first.get("/session")).status_code == 401
    assert (await second.get("/session")).status_code == 200


async def test_no_token_before_the_session_has_a_machine(night):
    _supervisor, _driver, http = night
    await _make_external(http)

    assert (await _ask(http)).status_code == 409  # no session yet

    async with http.db() as session:
        session.add(
            PolicySession(
                campaign_id=1, policy_id=1, status=PolicySessionStatus.PENDING.value,
                window_start=datetime.now(UTC),
            )
        )
        await session.commit()
    waiting = await _ask(http)
    assert waiting.status_code == 409
    assert "waiting for a machine" in waiting.json()["detail"]


async def test_a_policy_the_platform_runs_has_no_token_to_ask_for(night):
    supervisor, driver, http = night
    supervisor.tick()
    supervisor.tick()
    assert driver.workloads, "this one is a container"

    refused = await _ask(http)
    assert refused.status_code == 409
    assert "not external" in refused.json()["detail"]


async def test_only_the_owner_or_an_admin_may_ask(night):
    supervisor, _driver, http = night
    await _make_external(http)
    async with http.db() as session:
        session.add(User(id=2, username="other", password_hash="x"))
        session.add(User(id=3, username="root", password_hash="x", role="admin"))
        await session.commit()
    supervisor.tick()
    supervisor.tick()

    assert (await _ask(http, user_id=2)).status_code == 403
    assert (await _ask(http, user_id=3)).status_code == 200


async def test_a_policy_that_never_calls_in_fails_the_start(night, monkeypatch):
    supervisor, _driver, http = night
    await _make_external(http)
    supervisor.tick()
    supervisor.tick()
    async with http.db() as session:
        row = (await session.execute(select(PolicySession))).scalars().one()
        row.started_at = datetime.now(UTC) - timedelta(
            minutes=supervisor.settings.policy_start_timeout_minutes + 1
        )
        await session.commit()

    supervisor.tick()

    row = await _session_row(http)
    assert row.status != PolicySessionStatus.STARTING.value
    assert row.failure_class == "ready_timeout"
    assert "session-token" in row.error


async def test_a_policy_that_goes_silent_ends_its_search(night):
    supervisor, _driver, http = night
    await _make_external(http)
    await _open(supervisor, http)
    async with http.db() as session:
        row = (await session.execute(select(PolicySession))).scalars().one()
        row.last_heartbeat_at = datetime.now(UTC) - timedelta(hours=3)
        await session.commit()

    supervisor.tick()

    row = await _session_row(http)
    assert row.search_end_reason == "policy_wedged"
    assert row.status != PolicySessionStatus.SEARCHING.value


def test_a_registered_policy_is_a_container_or_external():
    with pytest.raises(ValidationError, match="image"):
        PolicyIn(name="no-image")
    assert PolicyIn(name="outside", external=True).image == ""
    with pytest.raises(ValidationError, match="no container"):
        PolicyIn(name="outside", external=True, gpus_in_container=True)
    with pytest.raises(ValidationError, match="no container"):
        PolicyIn(name="outside", external=True, needs_model=True)


async def test_it_is_registered_through_the_api_with_no_image(night):
    _supervisor, _driver, http = night
    async with http.db() as session:
        bearer = create_token(await session.get(User, 1))
    headers = {"Authorization": f"Bearer {bearer}"}

    made = await http.post(
        "/api/policies", json={"name": "my-notebook", "external": True}, headers=headers
    )
    assert made.status_code == 201, made.text
    assert made.json()["external"] is True and made.json()["image"] == ""

    no_image = await http.post("/api/policies", json={"name": "nothing"}, headers=headers)
    assert no_image.status_code == 422
    listed = await http.get("/api/policies", headers=headers)
    assert {p["name"]: p["external"] for p in listed.json()} == {
        "tpe-test": False, "my-notebook": True,
    }


async def _service_key(http, user_id: int = 1) -> str:
    from app.core import apikeys

    token, prefix, key_hash = apikeys.mint()
    async with http.db() as session:
        session.add(ApiKey(name="svc", prefix=prefix, key_hash=key_hash, owner_id=user_id))
        await session.commit()
    return token


async def test_a_service_key_registers_the_policy_and_asks_for_the_token(night):
    supervisor, _driver, http = night
    key = {"X-API-Key": await _service_key(http)}

    made = await http.post(
        "/api/policies", json={"name": "from-a-service", "external": True}, headers=key
    )
    assert made.status_code == 201, made.text
    assert "from-a-service" in [p["name"] for p in (await http.get("/api/policies",
                                                                  headers=key)).json()]

    await _make_external(http)
    supervisor.tick()
    supervisor.tick()
    issued = await http.post("/api/campaigns/1/session-token", headers=key)
    assert issued.status_code == 200, issued.text
    async with http.db() as session:
        event = (
            await session.execute(
                select(Event).where(Event.kind == "policy_session_token_issued")
            )
        ).scalars().one()
        assert event.actor == "key:svc"


async def test_a_launch_that_names_no_cards_is_given_free_ones(night):
    supervisor, _driver, http = night
    await _make_external(http)
    policy = await _open(supervisor, http)

    first = await policy.post("/launches", json={"engine_args": {"tp_size": 2}})
    second = await policy.post("/launches", json={"engine_args": {"tp_size": 4}})

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert len(first.json()["gpu_indices"]) == 2
    assert len(second.json()["gpu_indices"]) == 4
    assert not set(first.json()["gpu_indices"]) & set(second.json()["gpu_indices"])
    held = (await policy.get(f"/launches/{first.json()['id']}")).json()["gpu_indices"]
    assert held == first.json()["gpu_indices"]
