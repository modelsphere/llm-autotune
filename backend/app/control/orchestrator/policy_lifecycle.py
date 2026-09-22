"""Policy-session clock and command logic, shared by API and worker.

The same split as lifecycle.py: pure functions over rows, importable from the
async API (which reads) and the sync worker (which writes). Two rules keep the
two processes from fighting:

  * `policy_sessions.status` is written ONLY by the worker. The API records
    what the policy said (heartbeats, finalized_at, the plan) in the columns
    it owns and *derives* the command to answer with — so a heartbeat can say
    "finalize" the moment the deadline passes, one tick before the worker
    flips the status.
  * Deadlines are computed, never stored. Force-start rewrites the campaign
    window; a frozen copy would tell the policy a clock the platform is no
    longer keeping.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.control.orchestrator.lifecycle import as_utc, now
from app.core.config import get_settings
from app.db.models import (
    Campaign,
    ContenderStatus,
    Machine,
    PolicySession,
    PolicySessionStatus,
)
from app.schemas.policy import PolicySettings

# Contenders the platform still intends to validate.
PENDING_CONTENDER_STATES = {ContenderStatus.REGISTERED.value, ContenderStatus.SERVING.value}


def settings_of(campaign: Campaign) -> PolicySettings:
    """The campaign's policy knobs, defaults applied. Tolerant on read — a row
    written before a field existed must not strand its campaign."""
    try:
        return PolicySettings.model_validate(campaign.policy_settings or {})
    except Exception:
        return PolicySettings()


def heartbeat_timeout_seconds(campaign: Campaign) -> int:
    return settings_of(campaign).heartbeat_timeout_s or int(
        get_settings().policy_heartbeat_timeout_seconds
    )


def hard_deadline(campaign: Campaign, machine: Machine | None) -> datetime | None:
    """The end of the night: window end, capped by the lease if that is
    sooner. None means no clock is running (a campaign with no window is not
    startable as a policy campaign — refused at activation)."""
    ends = [as_utc(campaign.window_end)]
    if machine is not None:
        ends.append(as_utc(machine.lease_due_at))
    known = [e for e in ends if e is not None]
    return min(known) if known else None


def search_deadline(campaign: Campaign, machine: Machine | None) -> datetime | None:
    """When exploration must stop: the hard deadline minus the reserved
    validation time. A force-stop moves the *window*, so this follows it."""
    hard = hard_deadline(campaign, machine)
    if hard is None:
        return None
    return hard - timedelta(minutes=settings_of(campaign).reserve_minutes())


def deadlines(
    campaign: Campaign, machine: Machine | None
) -> tuple[datetime | None, datetime | None]:
    return search_deadline(campaign, machine), hard_deadline(campaign, machine)


def heartbeat_silence(session: PolicySession, moment: datetime | None = None) -> float:
    """Seconds since the policy was last heard from (or since start, before
    the first beat). Infinite silence would mean "never started", which the
    startup timeout owns — so this returns 0 until there is a reference point."""
    reference = as_utc(session.last_heartbeat_at) or as_utc(session.started_at)
    if reference is None:
        return 0.0
    return ((moment or now()) - reference).total_seconds()


def command_for(
    session: PolicySession,
    campaign: Campaign,
    machine: Machine | None,
    pending_contenders: int,
    serving_port: int | None = None,
) -> dict[str, Any]:
    """What this heartbeat should tell the policy to do.

    Derived, not stored: the API answers with this while the worker is between
    ticks, and the worker's own state machine advances `status` to agree on
    its next pass. The two can only disagree for one tick, and always in the
    safe direction (the policy is told to wind down early, never late).
    """
    search, hard = deadlines(campaign, machine)
    out: dict[str, Any] = {"search_deadline": search, "hard_deadline": hard}

    if session.abort_requested_at is not None:
        out["command"] = "abort"
        return out

    status = session.status
    if status == PolicySessionStatus.VALIDATING.value:
        if session.serving_contender_id is not None:
            out["command"] = "serve"
            out["contender_id"] = session.serving_contender_id
            out["port"] = serving_port
        elif pending_contenders > 0:
            # Between contenders: the worker is measuring or about to command
            # the next serve. Nothing for the policy to do but stay alive.
            out["command"] = "run"
        else:
            out["command"] = "exit"
        return out

    if status == PolicySessionStatus.FINALIZING.value:
        out["command"] = "exit" if session.finalized_at is not None else "finalize"
        return out

    # PENDING/STARTING/SEARCHING: the deadline is authoritative even before
    # the worker notices it. A policy that has explicitly signalled it is done
    # ("exhausted") or has given up ("error") is told to finalize this beat,
    # rather than waiting out the clock — the worker agrees on its next tick.
    past_deadline = search is not None and now() >= search
    signalled_done = session.policy_status in ("exhausted", "error")
    if session.finalize_requested_at is not None or past_deadline or signalled_done:
        out["command"] = "finalize"
    else:
        out["command"] = "run"
    return out
