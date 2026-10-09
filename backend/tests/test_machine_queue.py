"""The machine queue, and policy sessions sharing one machine.

`Supervisor._schedule` is one queue, oldest waiter first: campaigns with a
run ready, policy sessions without a machine, and what plugins add. A waiter
that cannot fit a machine it could use holds it for the rest of the pass.

A policy session with `share_machine` on takes only the slice its widest
candidate needs and a disjoint port block, so several can run side by side on
one node. Both sides must opt in, exactly as classic co-tenancy does.

Drives the supervisor tick against an in-memory platform and reads the
session rows it reserves.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import plugins
from app.control.orchestrator.occupancy import Reservation, busy_reason
from app.control.orchestrator.supervisor import Supervisor
from app.db.base import Base
from app.db.models import (
    Campaign,
    CampaignStatus,
    Machine,
    MachineState,
    Policy,
    PolicySession,
    PolicySessionStatus,
    User,
)
from app.plugins import PLUGIN_API_VERSION, Plugin, QueueWaiter
from tests.fakes import NullDriver


def _space(tp: int) -> dict:
    return {"base": {"tp": tp}, "grid": {"mem_fraction_static": [0.85, 0.9]}}


def _entrant(tp: int, *, share: bool = True) -> dict:
    """A policy campaign; a sharing one carries the cards its widest
    candidate needs (`policy_settings.cards`)."""
    return {"space": _space(tp), "share": share, "cards": tp if share else None}


def _stack(campaigns: list[dict], *, gpu_count: int = 8, ports: int = 4):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    with factory() as s:
        s.add(User(id=1, username="u", password_hash="x"))
        s.add(Policy(id=1, owner_id=1, name="rs", image="policy:1", ports=ports))
        s.add(Machine(id=1, name="node-1", host="10.0.0.1", gpu_count=gpu_count,
                      state=MachineState.AVAILABLE.value))
        for i, spec in enumerate(campaigns):
            s.add(Campaign(
                id=100 + i, owner_id=1, name=f"c{i}", engine="sglang", image="img",
                model_path="/m", served_model_name="m", search_space=spec["space"],
                objective={"target_metric": "score_total"}, machine_names=["node-1"],
                status=CampaignStatus.ACTIVE.value, run_baseline_canary=False,
                window_start=now - timedelta(minutes=1), window_end=now + timedelta(hours=4),
                policy_id=1, policy_settings={
                    "max_contenders": 1, "approx_minutes_each": 5,
                    **({"cards": spec["cards"]} if spec.get("cards") else {}),
                },
                share_machine=spec.get("share", False), service_port=28200,
            ))
        s.commit()
    sup = Supervisor(session_factory=factory)
    sup.driver = NullDriver()
    sup.settings.public_api_url = "http://platform.test"
    return sup, factory


def _sessions(factory) -> dict[int, PolicySession]:
    with factory() as s:
        rows = s.scalars(select(PolicySession).order_by(PolicySession.id)).all()
        return {r.campaign_id: r for r in rows}


def test_sharing_sessions_take_disjoint_card_and_port_slices():
    sup, factory = _stack([_entrant(1), _entrant(2), _entrant(4)])
    for _ in range(3):
        sup.tick()
    rows = _sessions(factory)
    assert {c: r.machine_id for c, r in rows.items()} == {100: 1, 101: 1, 102: 1}
    # Each session holds exactly the cards its widest candidate needs...
    assert rows[100].gpu_indices == [0]
    assert rows[101].gpu_indices == [1, 2]
    assert rows[102].gpu_indices == [3, 4, 5, 6]
    # ...and a port block of its own, walked up from the shared base port.
    assert rows[100].ports == [28200, 28201, 28202, 28203]
    assert rows[101].ports == [28204, 28205, 28206, 28207]
    assert rows[102].ports == [28208, 28209, 28210, 28211]
    assert all(r.status != PolicySessionStatus.PENDING.value for r in rows.values())


def test_a_session_that_does_not_share_still_owns_the_whole_box():
    sup, factory = _stack([_entrant(1, share=False), _entrant(2)])
    for _ in range(3):
        sup.tick()
    rows = _sessions(factory)
    assert rows[100].gpu_indices == list(range(8))
    # The sharing campaign may not join a box whose tenant wanted it alone.
    assert rows[101].machine_id is None
    assert rows[101].status == PolicySessionStatus.PENDING.value


def test_a_sharing_session_waits_when_the_cards_are_spoken_for():
    # 4 + 4 fill the box; the tp=2 session waits.
    sup, factory = _stack([_entrant(4), _entrant(4), _entrant(2)])
    for _ in range(3):
        sup.tick()
    rows = _sessions(factory)
    assert rows[100].gpu_indices == [0, 1, 2, 3]
    assert rows[101].gpu_indices == [4, 5, 6, 7]
    assert rows[102].machine_id is None
    assert rows[102].status == PolicySessionStatus.PENDING.value


def test_a_sharing_session_never_joins_a_tenant_that_did_not_opt_in():
    # Order reversed: the sharing tp=2 campaign reserves first, then the
    # exclusive one asks — an exclusive campaign needs an EMPTY box.
    sup, factory = _stack([_entrant(2), _entrant(1, share=False)])
    for _ in range(3):
        sup.tick()
    rows = _sessions(factory)
    assert rows[100].gpu_indices == [0, 1]
    assert rows[101].machine_id is None


def test_a_sharing_campaign_without_a_slice_keeps_the_whole_box():
    # Campaigns share by default; without `cards` a session still takes every
    # card, so a night that has the machine to itself is not shrunk.
    sup, factory = _stack([{"space": _space(2), "share": True}])
    sup.tick()
    rows = _sessions(factory)
    assert rows[100].gpu_indices == list(range(8))


# ------------------------------------------------------- plugins in the queue


def _holding(campaign_id: int, *, cards: int = 8, share: bool = False):
    """A plugin that holds node-1 for a campaign whose run is not placed yet."""

    def reservations(session, machine):
        if machine.name != "node-1":
            return []
        return [Reservation(campaign_id=campaign_id, label="request #7", cards=cards,
                            share=share)]

    return Plugin(name="holder", api_version=PLUGIN_API_VERSION, reservations=reservations)


def test_a_session_does_not_take_a_machine_a_plugin_holds(monkeypatch):
    sup, factory = _stack([_entrant(2)])
    monkeypatch.setattr(plugins, "enabled", lambda: (_holding(999),))
    for _ in range(3):
        sup.tick()
    assert _sessions(factory)[100].machine_id is None


def test_a_sharing_session_leaves_a_sharing_holds_cards_alone(monkeypatch):
    sup, factory = _stack([_entrant(2)])
    monkeypatch.setattr(plugins, "enabled", lambda: (_holding(999, cards=4, share=True),))
    for _ in range(3):
        sup.tick()
    # Eight cards, four held for the request: the session takes two of the rest.
    assert _sessions(factory)[100].gpu_indices == [0, 1]


def test_busy_reason_names_who_a_machine_is_held_for(monkeypatch):
    sup, factory = _stack([])
    monkeypatch.setattr(plugins, "enabled", lambda: (_holding(999),))
    with factory() as s:
        machine = s.get(Machine, 1)
        assert busy_reason(s, machine, cards=2, share=False) == "held for request #7"
        assert busy_reason(s, machine, cards=2, share=False, campaign_id=999) is None


def test_an_older_plugin_waiter_holds_the_machine_against_younger_work(monkeypatch):
    sup, factory = _stack([_entrant(2)])
    turns: list[str] = []

    def try_admit(session, blocked, busy):
        turns.append("waiter")
        busy.add(1)  # it could use node-1 once it frees; nobody behind may take it
        return False

    older = QueueWaiter(arrival=datetime(2020, 1, 1, tzinfo=UTC), try_admit=try_admit,
                        label="old request")
    monkeypatch.setattr(plugins, "enabled", lambda: (
        Plugin(name="w", api_version=PLUGIN_API_VERSION,
               queue_waiters=lambda supervisor, session: [older]),
    ))
    for _ in range(3):
        sup.tick()

    assert turns, "the waiter got its turns"
    assert _sessions(factory)[100].machine_id is None, "the younger session waited behind it"


def test_a_waiter_that_fails_is_contained(monkeypatch, caplog):
    sup, factory = _stack([_entrant(2)])

    def broken(session, blocked, busy):
        raise RuntimeError("boom")

    waiter = QueueWaiter(arrival=datetime(2020, 1, 1, tzinfo=UTC), try_admit=broken,
                         label="broken request")
    monkeypatch.setattr(plugins, "enabled", lambda: (
        Plugin(name="w", api_version=PLUGIN_API_VERSION,
               queue_waiters=lambda supervisor, session: [waiter]),
    ))
    for _ in range(3):
        sup.tick()

    assert "queue waiter broken request failed" in caplog.text
    assert _sessions(factory)[100].gpu_indices == [0, 1], "everyone else still got served"


def test_a_plugin_can_say_when_a_campaign_joined_the_queue(monkeypatch):
    from app.control.orchestrator.machine_queue import waiting_since

    sup, factory = _stack([_entrant(2)])
    asked = datetime(2021, 6, 1, tzinfo=UTC)
    monkeypatch.setattr(plugins, "enabled", lambda: (
        Plugin(name="a", api_version=PLUGIN_API_VERSION,
               queue_arrival=lambda session, c: asked if c.id == 100 else None),
    ))
    with factory() as s:
        assert waiting_since(s, s.get(Campaign, 100)) == asked
