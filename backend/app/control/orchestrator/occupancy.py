"""Who holds a machine, and whether one more run may go on it.

The machine queue (Supervisor._schedule) hands cards out one run at a time,
oldest waiter first. Two things hold cards while it does:

- RUNS. A live run holds the cards its nodes recorded; a run still tearing
  down holds them until the janitor confirms its container gone; a run that
  recorded no cards holds the whole machine (the baseline canary, legacy rows).
- RESERVATIONS. A campaign admitted to a machine before its first run exists
  holds it from admission until that run is placed, so the gap between "this
  machine is yours" and "your run is placed" (one tick for planning, longer
  behind a dataset pin) cannot be filled by anyone else. Plugins that admit
  campaigns to machines declare these (`Plugin.reservations`).

Sharing is opt-in on BOTH sides: a run joins a machine only if it shares and
everything already holding the machine shares too.

One module so that admission and placement read the same answer. When they
read different ones, a campaign is admitted to a machine the scheduler will
not place it on, and sits with nothing running.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.control.orchestrator.lifecycle import live_runs_on, teardown_pending_on
from app.db.models import Campaign, Machine

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Reservation:
    """A machine held for a campaign whose run is not placed yet."""

    campaign_id: int
    label: str  # who it is held for, as a person reads it: "submission #12"
    cards: int  # per node
    share: bool  # whether others may run next to it


def reservations_on(
    session: Session, machine: Machine, *, exclude_campaign: int | None = None
) -> list[Reservation]:
    """Every reservation on this machine, from every enabled plugin."""
    from app import plugins

    out: list[Reservation] = []
    for plugin in plugins.enabled():
        if plugin.reservations is None:
            continue
        try:
            found = list(plugin.reservations(session, machine))
        except Exception:
            logger.exception("plugin %s: reading reservations failed", plugin.name)
            continue
        out.extend(r for r in found if r.campaign_id != exclude_campaign)
    return out


def holds(session: Session, machine: Machine, campaign_id: int) -> bool:
    """Does this campaign hold a reservation on this machine? An admitted
    campaign is first in line for the machine it was admitted to — the queue's
    "held for an older waiter" never applies to it there, or a waiter that is
    itself blocked by the reservation would lock the owner out of it."""
    return any(r.campaign_id == campaign_id for r in reservations_on(session, machine))


def busy_reason(
    session: Session,
    machine: Machine,
    *,
    cards: int,
    share: bool,
    campaign_id: int | None = None,
) -> str | None:
    """Why a run of `cards` per node cannot go on this machine right now, or
    None if it can. Only occupancy: whether the machine is usable at all
    (leased, cleared, the right hardware) is the caller's question."""
    live = [r for r in live_runs_on(session, machine) if r.campaign_id != campaign_id]
    dying = teardown_pending_on(session, machine)
    held = reservations_on(session, machine, exclude_campaign=campaign_id)
    if not share:
        if live or dying:
            return "a run is using it"
        if held:
            return f"held for {held[0].label}"
        return None
    sharers = session.scalars(
        select(Campaign).where(Campaign.id.in_({r.campaign_id for r in live} or {0}))
    ).all()
    if any(not c.share_machine for c in sharers):
        return "a run that does not share is using it"
    exclusive = next((r for r in held if not r.share), None)
    if exclusive is not None:
        return f"held for {exclusive.label}"
    if any(not (r.gpu_indices or []) for r in live + dying):
        return "a run of unknown width holds the whole machine"
    taken = {i for r in live + dying for i in (r.gpu_indices or [])}
    free = machine.gpu_count - len(taken) - sum(r.cards for r in held)
    if free < cards:
        return f"only {max(free, 0)} card(s) free of {machine.gpu_count}, needs {cards}"
    return None
