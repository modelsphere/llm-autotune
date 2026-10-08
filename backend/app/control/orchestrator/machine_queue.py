"""The machine queue's order: who has been waiting longest.

Supervisor._schedule hands machines out oldest waiter first. Waiters, and
when each joined:
- an ACTIVE campaign with a run ready to place — after its last placement,
  and not before its work existed (oldest ready candidate, window opening),
  unless a plugin says when it arrived (`Plugin.queue_arrival`);
- a pending policy session with no machine — when its window opened;
- whatever a plugin adds (`Plugin.queue_waiters`) — when it says.

Ties at the same instant break by rank, then id: a plugin's waiter (rank 0
by default) before a campaign (1) before a policy session (2).
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.control.orchestrator.lifecycle import as_utc, now
from app.db.models import Campaign, Candidate, CandidateStatus, PolicySession, Run

logger = logging.getLogger(__name__)

CAMPAIGN, SESSION = 1, 2


def _arrival_from_plugins(session: Session, campaign: Campaign) -> datetime | None:
    from app import plugins

    for plugin in plugins.enabled():
        if plugin.queue_arrival is None:
            continue
        try:
            arrived = plugin.queue_arrival(session, campaign)
        except Exception:
            logger.exception("plugin %s: reading a campaign's arrival failed", plugin.name)
            continue
        if arrived is not None:
            return arrived
    return None


def waiting_since(session: Session, campaign: Campaign) -> datetime:
    last = session.scalars(
        select(func.max(Run.started_at)).where(Run.campaign_id == campaign.id)
    ).first()
    arrived = _arrival_from_plugins(session, campaign)
    if arrived is not None:
        base = [arrived]
    else:
        ready = session.scalars(
            select(func.min(Candidate.created_at)).where(
                Candidate.campaign_id == campaign.id,
                Candidate.status == CandidateStatus.VALID.value,
            )
        ).first()
        base = [ready, campaign.window_start]
    moments = [as_utc(m) for m in (last, *base) if m is not None]
    return max(moments) if moments else now()


def campaign_key(session: Session, campaign: Campaign) -> tuple:
    return (waiting_since(session, campaign), CAMPAIGN, campaign.id)


def session_key(row: PolicySession) -> tuple:
    return (as_utc(row.window_start) or now(), SESSION, row.id)


def waiter_key(waiter) -> tuple:
    """A plugin's QueueWaiter, in the same order as the platform's own."""
    return (as_utc(waiter.arrival) or now(), waiter.rank, waiter.ident)
