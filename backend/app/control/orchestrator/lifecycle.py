"""Where a machine is in its lease, and what moves it next.

The supervisor decides this on every tick in order to *act*. The Resources
page needs the same answer in order to *show* it. Computing it twice would
drift, so the predicates live here and both callers read them.

A lease is the hand-over: whoever leases a machine to the platform gives it
over free, and the platform runs campaigns on it until the lease ends. It
never stops or restarts anything it did not launch.

Nothing in this module touches ssh or changes anything: it reads rows and
returns a description.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.control.orchestrator.timing import run_minutes, window_minutes
from app.control.run_nodes import run_ids_on_machine
from app.db.models import (
    TERMINAL_RUN_STATES,
    Campaign,
    CampaignStatus,
    Candidate,
    CandidateStatus,
    LeaseEndMode,
    LeaseState,
    Machine,
    MachineState,
    Run,
)
from app.staging import SCREEN, stage_of_run


def now() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime | None) -> datetime | None:
    """Postgres hands back tz-aware datetimes, sqlite naive ones. Comparing
    the two raises, so normalize before any time arithmetic."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


# Written onto a run the user stopped by hand. The lifecycle reads it back:
# re-scheduling one tick later would silently undo their Stop.
USER_STOP_ERROR = "stopped by user"


# -- predicates ---------------------------------------------------------------


def campaigns_using(session: Session, machine: Machine) -> list[Campaign]:
    """Campaigns whose window is a live instruction about this machine.

    SCHEDULED is included even though such a campaign is asleep: it will want
    the machine again at its next window.

    PAUSED is deliberately EXCLUDED: a paused campaign is frozen — the platform
    must not act on the machine on its behalf. Its lease is frozen too (see
    `frozen_by_pause`), so the machine is left exactly as it is until a human
    resumes or ends the lease.
    """
    campaigns = session.scalars(
        select(Campaign).where(
            Campaign.status.in_(
                [
                    CampaignStatus.ACTIVE.value,
                    CampaignStatus.SCHEDULED.value,
                    CampaignStatus.DONE.value,
                ]
            )
        )
    ).all()
    return [c for c in campaigns if not c.machine_names or machine.name in c.machine_names]


# -- the lease ----------------------------------------------------------------


def accepts_new_work(machine: Machine) -> bool:
    """May a run START on this machine right now?

    Distinct from "is it ours": a draining machine is still ours and still
    finishing runs, but every new placement on it is one the lease holder has
    already been told will not happen.
    """
    if machine.state == MachineState.AWAY.value:
        return False
    return machine.lease_state != LeaseState.DRAINING.value


def lease_expired(machine: Machine) -> bool:
    """Past the time we promised the machine back, with nobody having asked.

    A lease that lapses quietly is worse than one that ends loudly: the holder
    planned around getting it back, so the platform drains itself rather than
    waiting to be told.
    """
    if machine.lease_state != LeaseState.ACTIVE.value:
        return False
    due = as_utc(machine.lease_due_at)
    return due is not None and now() >= due


def lease_deadline_passed(machine: Machine) -> bool:
    deadline = as_utc(machine.lease_deadline_at)
    return deadline is not None and now() >= deadline


def drain_kills_running(machine: Machine) -> bool:
    """Should live runs be stopped rather than waited for?

    True for an eager end from the moment it is requested, and for a polite one
    only once its deadline arrives — a promise to be off by a time is still a
    promise when keeping it costs a benchmark.
    """
    if machine.lease_state != LeaseState.DRAINING.value:
        return False
    if machine.lease_end_mode == LeaseEndMode.EAGER.value:
        return True
    return lease_deadline_passed(machine)


# How long a machine may be held, when nobody said. Long enough for a night
# plus slack; short enough that a forgotten lease surfaces within a day.
DEFAULT_LEASE_HOURS = 24

BUSY = "busy"  # our runs are on it
IDLE = "idle"  # ours, nothing running
RETURNABLE = "returnable"  # not ours; take it whenever


def readiness(session: Session, machine: Machine) -> str:
    """The one-word answer an external fleet manager is asking for."""
    if live_runs_on(session, machine):
        return BUSY
    if machine.lease_state in (LeaseState.NONE.value, LeaseState.RELEASED.value):
        return RETURNABLE
    return IDLE


def returnable_at(session: Session, machine: Machine) -> datetime | None:
    """The worst case for when a polite hand-back completes.

    Computed from each live run's own cutoff — `max_run_minutes` from when it
    started — not from an average. A caller planning around this needs a bound
    it can rely on, and "usually 20 minutes" is not one.
    """
    live = live_runs_on(session, machine)
    if not live:
        return None
    latest = None
    for run in live:
        started = as_utc(run.started_at) or as_utc(run.created_at) or now()
        # The cap is the bound; what this campaign's runs have actually taken
        # is a tighter one, once there is any.
        cap = (run.campaign.max_run_minutes if run.campaign else 0) or 150
        learned = run_minutes(run.campaign, stage_of_run(run)) if run.campaign else None
        limit = min(cap, learned) if learned is not None else cap
        finish = started + timedelta(minutes=limit)
        latest = finish if latest is None else max(latest, finish)
    return latest


# -- machine readiness for a run that is about to be scheduled ----------------
#
# What is wrong, right now, with the machines a campaign is
# pinned to — computed from rows only (no ssh), so it can ride on every read of
# the object and be shown as a banner before a single container is launched.
# This is the cheap, always-available companion to the ssh preflight probe.

MACHINE_UNAVAILABLE = "unavailable"  # not registered, or not leased to us
MACHINE_EXPIRING = "expiring"        # leased, but the lease ends before the run would
MACHINE_EXPIRED = "expired"          # leased, but already past due — being handed back


@dataclass
class MachineWarning:
    machine: str  # "" for a fleet-wide finding (no pool pinned, nothing leased)
    status: str
    detail: str

    def as_dict(self) -> dict:
        return {"machine": self.machine, "status": self.status, "detail": self.detail}


def leased_target_count(machines: list[Machine], machine_names: list[str]) -> int:
    """How many of the pinned machines are actually leased to us right now. With
    no pin, the whole leased fleet counts — the scheduler may use any of them."""
    active = [m for m in machines if m.lease_state == LeaseState.ACTIVE.value]
    wanted = {n for n in (machine_names or []) if n}
    if not wanted:
        return len(active)
    return sum(1 for m in active if m.name in wanted)


def machine_warnings(
    machines: list[Machine],
    machine_names: list[str],
    *,
    finish_by: datetime | None = None,
) -> list[dict]:
    """Findings about the machines a run is pinned to, before it launches.

    `machines` is the fleet (already fetched by the caller, since this stays
    sync and ssh-free). `finish_by` is when the run is expected to be done: a
    lease due before then is flagged `expiring`. A machine with no due date
    never expires, so it never warns — which is the whole point of the new
    no-default-expiry lease.
    """
    by_name = {m.name: m for m in machines}
    wanted = [n for n in (machine_names or []) if n]
    warnings: list[MachineWarning] = []

    if not wanted:
        # No pool pinned: any leased machine will do. The only thing to warn
        # about is having none at all.
        if not any(m.lease_state == LeaseState.ACTIVE.value for m in machines):
            warnings.append(MachineWarning(
                "", MACHINE_UNAVAILABLE,
                "no machine is leased to the platform — lease one from the Resources page",
            ))
        return [w.as_dict() for w in warnings]

    for name in wanted:
        machine = by_name.get(name)
        if machine is None:
            warnings.append(MachineWarning(
                name, MACHINE_UNAVAILABLE,
                f"{name} is not registered — lease it from the Resources page",
            ))
            continue
        if machine.lease_state != LeaseState.ACTIVE.value:
            warnings.append(MachineWarning(
                name, MACHINE_UNAVAILABLE,
                f"{name} is not leased (it is {machine.state}); lease it before the run",
            ))
            continue
        due = as_utc(machine.lease_due_at)
        if due is None:
            continue  # held until ended by hand — never expires
        if now() >= due:
            warnings.append(MachineWarning(
                name, MACHINE_EXPIRED,
                f"{name}'s lease is past due and will be handed back on the next tick",
            ))
        elif finish_by is not None and due < finish_by:
            warnings.append(MachineWarning(
                name, MACHINE_EXPIRING,
                f"{name}'s lease is due {due:%Y-%m-%d %H:%M} UTC, before this run would "
                f"finish (~{finish_by:%Y-%m-%d %H:%M} UTC); it will be handed back mid-run",
            ))
    return [w.as_dict() for w in warnings]


def window_allows_run_of(campaign: Campaign, minutes: int) -> bool:
    """Is the window open, with `minutes` still to spare before it closes?

    Taken as a parameter rather than read off the campaign because the two
    stages of a staged campaign take very different amounts of time: a
    screening sweep fits where a replay does not, and starting the replay
    anyway means killing it at the cutoff having learned nothing.
    """
    moment = now()
    start, end = as_utc(campaign.window_start), as_utc(campaign.window_end)
    if start is not None and moment < start:
        return False
    if end is None:
        return True
    return moment + timedelta(minutes=minutes) <= end


def window_allows_new_run(campaign: Campaign, default_max_run_minutes: int) -> bool:
    """Is there room before the window closes to finish one more run?"""
    return window_allows_run_of(
        campaign,
        window_minutes(campaign, SCREEN, campaign.max_run_minutes, default_max_run_minutes),
    )


def has_valid_candidates(session: Session, campaign: Campaign) -> bool:
    return (
        session.scalars(
            select(Candidate)
            .where(
                Candidate.campaign_id == campaign.id,
                Candidate.status == CandidateStatus.VALID.value,
            )
            .limit(1)
        ).first()
        is not None
    )


def policy_campaign_wants_machine(session: Session, campaign: Campaign) -> bool:
    """Does this policy campaign still need a machine for its current window?

    Policy campaigns have no Candidate queue — their "work" is one container
    per window — so the has_valid_candidates gate reads them as idle and would
    never capture/clear production for them. The equivalent question is: does
    the current window lack a *terminal* session? (A live session still needs
    the machine; a terminal one is the night being over.)
    """
    if not campaign.policy_id:
        return False
    from app.db.models import TERMINAL_SESSION_STATES, PolicySession

    window = as_utc(campaign.window_start)
    rows = session.scalars(
        select(PolicySession).where(PolicySession.campaign_id == campaign.id)
    ).all()
    current = [r for r in rows if as_utc(r.window_start) == window]
    return not any(
        r.status in {s.value for s in TERMINAL_SESSION_STATES} for r in current
    )


def machine_has_live_session(session: Session, machine: Machine) -> bool:
    """Is a policy container (or its night) currently holding this machine?

    Sessions reserve a machine exclusively but are not Runs, so every gate
    that reasons from live runs — restore, drain, new-work admission — needs
    this second question asked alongside.
    """
    from app.db.models import TERMINAL_SESSION_STATES, PolicySession

    live = session.scalars(
        select(PolicySession)
        .where(
            PolicySession.machine_id == machine.id,
            PolicySession.status.not_in([s.value for s in TERMINAL_SESSION_STATES]),
        )
        .limit(1)
    ).first()
    return live is not None


def campaigns_waiting_on(
    session: Session, machine: Machine, default_max_run_minutes: int
) -> list[Campaign]:
    """Campaigns that would start work on this machine right now."""
    return [
        campaign
        for campaign in campaigns_using(session, machine)
        if campaign.status == CampaignStatus.ACTIVE.value
        and window_allows_new_run(campaign, default_max_run_minutes)
        and (
            has_valid_candidates(session, campaign)
            or policy_campaign_wants_machine(session, campaign)
        )
    ]


def frozen_by_pause(session: Session, machine: Machine) -> bool:
    """Is this machine frozen because the campaign holding it was paused?

    Pause must mean frozen. While a machine's only live claim is a paused
    campaign, the platform touches nothing: the lease does not auto-expire.
    The human is back in control — they resume (work continues) or end the
    lease explicitly (the machine is handed back the normal way). An ACTIVE or SCHEDULED campaign
    sharing the machine keeps normal expiry; only a pause with no other live
    claim freezes it. Explicit end-lease is unaffected: it sets the drain
    directly and never runs through here.
    """
    using = [
        c
        for c in session.scalars(
            select(Campaign).where(
                Campaign.status.in_(
                    [
                        CampaignStatus.ACTIVE.value,
                        CampaignStatus.SCHEDULED.value,
                        CampaignStatus.PAUSED.value,
                    ]
                )
            )
        ).all()
        if not c.machine_names or machine.name in c.machine_names
    ]
    if not any(c.status == CampaignStatus.PAUSED.value for c in using):
        return False
    return not any(
        c.status in (CampaignStatus.ACTIVE.value, CampaignStatus.SCHEDULED.value)
        for c in using
    )


# -- what ending the lease will actually do -----------------------------------


@dataclass(frozen=True)
class HandBack:
    """What End lease does, in one line, for the card and the confirm dialog."""

    summary: str

    def as_dict(self) -> dict:
        return {"summary": self.summary}


def hand_back(session: Session, machine: Machine) -> HandBack:
    live = live_runs_on(session, machine)
    if live:
        return HandBack(
            f"{len(live)} run(s) of ours stop (or finish, ending politely), then the "
            "machine goes back. Nothing else on it is touched."
        )
    return HandBack("Nothing of ours is running; the machine goes straight back.")


# -- the description the Resources page renders -------------------------------

# The sequence a machine walks. The page draws these as steps, so "what
# happens next" is a position rather than a paragraph.
STEPS = ("Leased", "Running campaigns", "Handed back")

WAITING = "waiting"  # nothing to do until something outside changes
WORKING = "working"  # the platform is moving this along on its own
BLOCKED = "blocked"  # it needs a human
DONE = "done"


@dataclass(frozen=True)
class MachineLifecycle:
    machine_id: int
    # The step now in play — everything before it has happened. When the state
    # is `waiting` this is the step being waited for, which is why the page
    # draws it hollow; when `working`, it is the step happening right now.
    step: int
    state: str
    headline: str
    detail: str
    campaigns: list[dict] = field(default_factory=list)
    # busy | idle | returnable — the answer an external lease holder wants.
    readiness: str = IDLE
    # Worst-case completion of a polite hand-back, or None if nothing is running.
    returnable_at: datetime | None = None
    # What End lease does on this machine right now. Never None.
    hand_back: HandBack = field(default_factory=lambda: HandBack(""))

    def as_dict(self) -> dict:
        return {
            "machine_id": self.machine_id,
            "step": self.step,
            "state": self.state,
            "headline": self.headline,
            "detail": self.detail,
            "campaigns": self.campaigns,
            "readiness": self.readiness,
            "returnable_at": (
                self.returnable_at.isoformat() if self.returnable_at else None
            ),
            "hand_back": self.hand_back.as_dict(),
        }


def _named(campaigns: list[Campaign]) -> list[dict]:
    return [{"id": c.id, "name": c.name} for c in campaigns]


def _clock(moment: datetime | None) -> str:
    return "" if moment is None else moment.strftime("%H:%M UTC")


def live_runs_on(session: Session, machine: Machine) -> list[Run]:
    """Runs currently occupying this machine, as master or as a worker.

    Membership comes from `run_nodes`, not `runs.machine_id`: a gang's worker
    machines are occupied by a run whose `machine_id` points at the master, and
    a query on the run row alone would call those machines free. For a
    single-node run the rank-0 node aliases `machine_id`, so the answer is the
    same either way.
    """
    return list(
        session.scalars(
            select(Run).where(
                Run.id.in_(run_ids_on_machine(machine.id)),
                Run.status.not_in([s.value for s in TERMINAL_RUN_STATES]),
            )
        ).all()
    )


def teardown_pending_on(session: Session, machine: Machine) -> list[Run]:
    """Terminal runs on this machine whose container is not confirmed gone.

    Finished for every other purpose, but still holding their cards and port
    until the janitor sees them vanish — so no placement may land on hardware a
    dying container has not released.
    """
    return list(
        session.scalars(
            select(Run).where(
                Run.id.in_(run_ids_on_machine(machine.id)),
                Run.teardown_pending.is_(True),
            )
        ).all()
    )


def describe(
    session: Session, machine: Machine, default_max_run_minutes: int
) -> MachineLifecycle:
    """One machine's position in its lease, in the words of what is true."""
    waiting = campaigns_waiting_on(session, machine, default_max_run_minutes)
    live = live_runs_on(session, machine)

    def out(step, state, headline, detail):
        return MachineLifecycle(
            machine_id=machine.id,
            step=step,
            state=state,
            headline=headline,
            detail=detail,
            campaigns=_named(waiting),
            readiness=readiness(session, machine),
            returnable_at=returnable_at(session, machine),
            hand_back=hand_back(session, machine),
        )

    if machine.state == MachineState.AWAY.value:
        if machine.lease_state == LeaseState.RELEASED.value:
            return out(2, DONE, "Handed back",
                       "The lease has ended. Lease it again to run campaigns here.")
        return out(0, WAITING, "Not leased",
                   "Lease it to let campaigns run here.")

    # Checked first because a drain overrides everything: a draining machine is
    # not waiting for a campaign, it is leaving.
    if machine.lease_state == LeaseState.DRAINING.value:
        eager = machine.lease_end_mode == LeaseEndMode.EAGER.value
        if live:
            if eager or lease_deadline_passed(machine):
                return out(2, WORKING, "Stopping runs to hand the machine back",
                           f"{len(live)} run(s) are being stopped.")
            until = returnable_at(session, machine)
            return out(2, WORKING, "Finishing up before hand-back",
                       f"{len(live)} run(s) still going and no new ones will start."
                       + (f" Free by {_clock(until)}." if until else ""))
        return out(2, WORKING, "Handing the machine back",
                   "Nothing is running; the lease closes on its own.")

    if live:
        cards = sum(len(r.gpu_indices or []) for r in live)
        return out(1, WORKING, f"{len(live)} run(s) in flight",
                   f"Campaigns hold {cards} of {machine.gpu_count} cards.")
    if waiting:
        return out(1, WORKING, "Ready",
                   "A campaign wants this machine; its runs start on the next tick.")
    return out(1, WAITING, "Leased, nothing to run",
               "No active campaign is pinned here with work left.")


def describe_all(session: Session, default_max_run_minutes: int) -> list[MachineLifecycle]:
    machines = session.scalars(select(Machine).order_by(Machine.id)).all()
    return [describe(session, m, default_max_run_minutes) for m in machines]
