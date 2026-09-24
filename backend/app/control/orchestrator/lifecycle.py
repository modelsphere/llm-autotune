"""Where a machine is in the hand-over sequence, and what moves it next.

The supervisor decides this on every tick in order to *act*. The Resources
page needs the same answer in order to *show* it. Computing it twice would
drift — and drift here is what made an operator press Capture and Clear by
hand, undoing the very canary those buttons exist to protect. So the
predicates live here and both callers read them.

Nothing in this module touches ssh or changes anything: it reads rows and
returns a description.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.control.run_nodes import run_ids_on_machine
from app.db.models import (
    TERMINAL_RUN_STATES,
    BaselineStatus,
    Campaign,
    CampaignStatus,
    Candidate,
    CandidateStatus,
    Event,
    LeaseEndMode,
    LeaseState,
    Machine,
    MachineState,
    Run,
    RunKind,
    RunStatus,
)


def now() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime | None) -> datetime | None:
    """Postgres hands back tz-aware datetimes, sqlite naive ones. Comparing
    the two raises, so normalize before any time arithmetic."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


# The baseline canary is bounded, counted from the campaign's last start:
# production that cannot be benchmarked is a finding to report, not something
# to keep re-measuring until morning.
MAX_CANARY_ATTEMPTS_PER_START = 3

# Written onto a run the user stopped by hand. The lifecycle reads it back:
# re-scheduling one tick later would silently undo their Stop.
USER_STOP_ERROR = "stopped by user"


# -- predicates ---------------------------------------------------------------


def campaigns_using(session: Session, machine: Machine) -> list[Campaign]:
    """Campaigns whose window is a live instruction about this machine.

    SCHEDULED is included even though such a campaign is asleep: a nightly job
    that stood down at 08:00 still owns the statement "hand production back at
    08:00", and dropping it from this list would leave the machine cleared with
    production down until someone noticed.

    PAUSED is deliberately EXCLUDED: a paused campaign is frozen — the platform
    must not act on the machine on its behalf, neither clearing nor restoring
    production. Its lease is frozen too (see `frozen_by_pause`), so the machine
    is left exactly as it is until a human resumes or ends the lease.
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
RETURNABLE = "returnable"  # production is back; take it whenever


def readiness(session: Session, machine: Machine) -> str:
    """The one-word answer an external fleet manager is asking for.

    Deliberately not derived from `state` alone: RESERVED means a run holds the
    machine, but a machine with no runs can still be un-returnable because
    production has not been put back yet. Handing back a box whose production
    service is still down is the failure this word exists to prevent.
    """
    if live_runs_on(session, machine):
        return BUSY
    if machine.lease_state in (LeaseState.NONE.value, LeaseState.RELEASED.value):
        return RETURNABLE
    if machine.baseline_status == BaselineStatus.CLEARED.value:
        return IDLE  # ours, free, but production is still down
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
        limit = (run.campaign.max_run_minutes if run.campaign else 0) or 150
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
        campaign, campaign.max_run_minutes or default_max_run_minutes
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


def last_cleared_at(session: Session, machine: Machine) -> datetime | None:
    """When production was last taken down on this machine.

    Read from the audit trail rather than stored on the machine: the clear is
    already an event, and a second copy of the same fact is a second thing to
    keep in step.
    """
    events = session.scalars(
        select(Event)
        .where(Event.kind.in_(["baseline_cleared", "baseline_cleared_auto"]))
        .order_by(Event.id.desc())
        .limit(50)
    ).all()
    for event in events:
        if (event.payload or {}).get("machine") == machine.name:
            return as_utc(event.ts)
    return None


def nothing_was_captured(machine: Machine) -> bool:
    """CLEARED, but from a capture that measured nothing.

    Two machines read `cleared` and mean opposite things. One had production
    stopped BY US and is owed it back; the other was already empty when it was
    handed over, so there is nothing to put back and never was. They differ
    only by whether the capture holds services — which is precisely what the
    hand-back path branches on, and precisely what the page did not say.
    """
    return not (machine.baseline or {}).get("services")


def restore_due(session: Session, machine: Machine) -> bool:
    """Is production owed back on this machine?

    Only a declared window triggers a restore: it is the "hand it back by"
    contract. Without one the session is supervised and a human decides, so we
    never yank a machine out from under someone still iterating.

    And only windows that closed AFTER the machine was cleared can be telling
    us to undo that clear. A campaign that finished days ago has already had
    its hand-back honoured; reading its expired window as a standing
    instruction restores production out from under whatever the machine was
    cleared for next — observed live, eight seconds after an operator cleared
    node-24 by hand, on the orders of a campaign done for four days.
    """
    cleared_at = last_cleared_at(session, machine)
    windowed = [
        c
        for c in campaigns_using(session, machine)
        if c.window_end is not None
        and (cleared_at is None or as_utc(c.window_end) >= cleared_at)
    ]
    if not windowed:
        return False
    return all(now() >= as_utc(c.window_end) for c in windowed)


def frozen_by_pause(session: Session, machine: Machine) -> bool:
    """Is this machine frozen because the campaign holding it was paused?

    Seen live: an operator meant to end a lease but hit Pause,
    reasonably expecting a paused campaign to leave the machine alone. It did
    not — the lease kept its own clock, expired at the force-start window's end,
    and (after a worker restart, hours late) auto-drained: production was torn
    back down and relaunched from the baseline, stomping the service the
    operator had restored by hand in the meantime.

    Pause must mean frozen. While a machine's only live claim is a paused
    campaign, the platform touches nothing: the lease does not auto-expire, so
    production is neither restored nor cleared on its own. The human is back in
    control — they resume (work continues) or end the lease explicitly (the
    machine is handed back the normal way). An ACTIVE or SCHEDULED campaign
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


# -- the baseline canary ------------------------------------------------------

CANARY_NOT_REQUIRED = "not_required"  # this campaign does not want one
CANARY_NOT_READY = "not_ready"  # nothing captured yet to measure
CANARY_PASSED = "passed"  # production measured; clearing is unblocked
CANARY_LIVE = "live"  # one is in flight
CANARY_DUE = "due"  # should be scheduled now
CANARY_STOPPED = "stopped"  # a human stopped it; restarting is how to resume
CANARY_EXHAUSTED = "exhausted"  # attempts used up without a pass


def last_activation(session: Session, campaign: Campaign) -> datetime:
    """When this campaign was last started, falling back to its creation."""
    events = session.scalars(
        select(Event)
        .where(Event.campaign_id == campaign.id, Event.kind == "campaign_status_changed")
        .order_by(Event.id.desc())
        .limit(20)
    ).all()
    for event in events:
        if (event.payload or {}).get("status") == CampaignStatus.ACTIVE.value:
            return as_utc(event.ts)
    return as_utc(campaign.created_at)


def canaries_since_activation(
    session: Session, campaign: Campaign, machine: Machine
) -> list[Run]:
    since = last_activation(session, campaign)
    canaries = session.scalars(
        select(Run)
        .where(
            Run.campaign_id == campaign.id,
            Run.id.in_(run_ids_on_machine(machine.id)),
            Run.kind == RunKind.BASELINE.value,
        )
        .order_by(Run.id)
    ).all()
    return [r for r in canaries if as_utc(r.created_at) >= since]


def canary_state(session: Session, campaign: Campaign, machine: Machine) -> str:
    """What the baseline canary owes this campaign on this machine.

    One canary per campaign+machine, ever, was too few: a canary that failed or
    was stopped left the campaign deadlocked forever, because clearing
    production requires a canary that PASSED and nothing would schedule
    another. Attempts are counted from the campaign's last start — pressing
    Start again is how a human says "try again".
    """
    if not campaign.run_baseline_canary:
        return CANARY_NOT_REQUIRED

    attempts = canaries_since_activation(session, campaign, machine)
    if any(r.status == RunStatus.SUCCEEDED.value for r in attempts):
        return CANARY_PASSED
    if any(r.status not in [s.value for s in TERMINAL_RUN_STATES] for r in attempts):
        return CANARY_LIVE
    if any(r.error == USER_STOP_ERROR for r in attempts):
        return CANARY_STOPPED
    if len(attempts) >= MAX_CANARY_ATTEMPTS_PER_START:
        return CANARY_EXHAUSTED
    # Only a captured machine has a production endpoint to point the benchmark
    # at, and only a non-empty capture has anything worth measuring.
    if machine.baseline_status != BaselineStatus.CAPTURED.value:
        return CANARY_NOT_READY
    if not (machine.baseline or {}).get("services"):
        return CANARY_NOT_READY
    return CANARY_DUE


# -- what ending the lease will actually do -----------------------------------


@dataclass(frozen=True)
class HandBack:
    """The consequence of pressing End lease, read off the branch the drain
    will actually take.

    The confirm dialog used to promise "production is restored either way",
    which was true of neither machine in testing: one was cleared from an
    empty capture, so there was nothing to put back; and with auto-restore off
    a real capture is handed back with production still DOWN and only an event
    to say so. A dialog that describes a different code path than the one about
    to run is worse than no dialog, so this mirrors `_advance_drain` field for
    field and both the card and the dialog read it.
    """

    restores: bool  # the platform starts production again before releasing
    owed: bool  # production is down and we are NOT the ones putting it back
    services: int  # how many captured services that verdict is about
    summary: str  # one line, shown on the card and in the confirm dialog

    def as_dict(self) -> dict:
        return {
            "restores": self.restores,
            "owed": self.owed,
            "services": self.services,
            "summary": self.summary,
        }


def hand_back(machine: Machine, auto_restore: bool) -> HandBack:
    """What End lease does to production on this machine, right now."""
    services = (machine.baseline or {}).get("services") or []
    count = len(services)

    if machine.baseline_status != BaselineStatus.CLEARED.value:
        return HandBack(
            False, False, count,
            "Production was never stopped here, so nothing on the machine is "
            "touched — the lease just closes.",
        )
    if not count:
        return HandBack(
            False, False, 0,
            "Nothing was captured on this machine — it was already free when it "
            "was handed over — so there is nothing to put back. The lease closes "
            "and the machine goes straight back.",
        )
    if auto_restore:
        return HandBack(
            True, False, count,
            f"{count} production service(s) are started again from the capture "
            "before the lease closes. The deploy script returns in seconds; the "
            "model then loads for minutes.",
        )
    return HandBack(
        False, True, count,
        f"Production stays DOWN. Auto-restore is off, so the {count} captured "
        "service(s) are not started again — putting them back is yours to do, "
        "from Override \u25b8 Restore (which keeps working after the lease closes).",
    )


# -- the description the Resources page renders -------------------------------

# The sequence a machine walks each night. The page draws these as steps, so
# "what happens next" is a position rather than a paragraph.
STEPS = ("Leased", "Captured", "Baseline measured", "Cleared", "Restored")

WAITING = "waiting"  # nothing to do until something outside changes
WORKING = "working"  # the platform is moving this along on its own
BLOCKED = "blocked"  # it needs a human
DONE = "done"


@dataclass(frozen=True)
class MachineLifecycle:
    machine_id: int
    # The step now in play — everything before it has happened. When the state
    # is `waiting` this is the step that has NOT happened yet and is being
    # waited for, which is why the page draws it hollow; when `working`, it is
    # the step happening right now.
    step: int
    state: str
    headline: str
    detail: str
    campaigns: list[dict] = field(default_factory=list)
    # A canary is owed and has not passed. Clearing production by hand right
    # now destroys the service the canary exists to measure.
    canary_pending: bool = False
    # busy | idle | returnable — the answer an external lease holder wants.
    readiness: str = IDLE
    # Worst-case completion of a polite hand-back, or None if nothing is running.
    returnable_at: datetime | None = None
    # What End lease does to production on this machine, from the same branch
    # the drain will take. Never None, so no caller has to guess a default.
    hand_back: HandBack = field(
        default_factory=lambda: HandBack(False, False, 0, "")
    )

    def as_dict(self) -> dict:
        return {
            "machine_id": self.machine_id,
            "step": self.step,
            "state": self.state,
            "headline": self.headline,
            "detail": self.detail,
            "campaigns": self.campaigns,
            "canary_pending": self.canary_pending,
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
    session: Session,
    machine: Machine,
    default_max_run_minutes: int,
    auto: bool = True,
    auto_restore: bool = False,
) -> MachineLifecycle:
    """One machine's position in the sequence, in the words of what is true."""
    status = machine.baseline_status
    waiting = campaigns_waiting_on(session, machine, default_max_run_minutes)
    live = live_runs_on(session, machine)
    # A drain is carried by `_advance_leases`, which is NOT gated on the
    # baseline-lifecycle switch: a machine being handed back keeps moving on
    # its own even with automation off. Every other step stops dead without it.
    self_driving = auto or machine.lease_state == LeaseState.DRAINING.value

    def out(step, state, headline, detail, canary_pending=False):
        shown_state, shown_detail = state, detail
        if not self_driving and state == WORKING:
            # With the lifecycle switched off, a WORKING step is a lie: nothing
            # is happening and nothing will. Say so — but keep the step and the
            # headline, because WHERE the machine is stayed true the whole time.
            # Collapsing all of it to a single "automation is off" banner at
            # step 0 is what left two cleared machines reading as freshly
            # leased, with no way to tell that their capture was empty.
            shown_state = BLOCKED
            shown_detail = (
                f"{detail} Automatic hand-over is off "
                "(AUTOTUNE_AUTO_BASELINE_LIFECYCLE=false), so this step will not "
                "happen on its own — drive it by hand from the Override menu."
            )
        return MachineLifecycle(
            machine_id=machine.id,
            step=step,
            state=shown_state,
            headline=headline,
            detail=shown_detail,
            campaigns=_named(waiting),
            canary_pending=canary_pending,
            readiness=readiness(session, machine),
            returnable_at=returnable_at(session, machine),
            hand_back=hand_back(machine, auto_restore),
        )

    if machine.state == MachineState.AWAY.value:
        if status == BaselineStatus.RESTORED.value:
            return out(4, DONE, "Returned to production",
                       "Production is back up and the machine is out of the pool.")
        return out(0, WAITING, "Held by production",
                   "Lease it to hand the machine to the platform for a night.")

    # -- being handed back ----------------------------------------------------
    #
    # Checked before the baseline states because a drain overrides all of them:
    # a draining machine is not "waiting for a campaign", it is leaving.
    if machine.lease_state == LeaseState.DRAINING.value:
        eager = machine.lease_end_mode == LeaseEndMode.EAGER.value
        if live:
            cards = sum(len(r.gpu_indices or []) for r in live)
            if eager or lease_deadline_passed(machine):
                return out(4, WORKING, "Stopping runs to hand the machine back",
                           f"{len(live)} run(s) on {cards} cards are being killed. "
                           "Production goes back as soon as they are down.")
            return out(3, WORKING, "Finishing up before hand-back",
                       f"{len(live)} run(s) still going and no new ones will start. "
                       + (f"Free by {_clock(returnable_at(session, machine))}."
                          if returnable_at(session, machine) else ""))
        return out(4, WORKING, "Handing the machine back",
                   "Nothing is running. Production is being restored, then the "
                   "lease closes on its own.")

    # -- borrowed, production untouched --------------------------------------
    if status in (BaselineStatus.NONE.value, BaselineStatus.RESTORED.value):
        if not waiting:
            if status == BaselineStatus.RESTORED.value:
                return out(4, DONE, "Production restored",
                           "The night is over. Return the machine, or start another "
                           "campaign and the sequence begins again.")
            return out(1, WAITING, "Leased, nothing to run",
                       "No active campaign is pinned here with candidates left. "
                       "Production keeps running until one is.")
        return out(1, WORKING, "Recording production",
                   "Listing what is deployed and how to bring it back. "
                   "Nothing is stopped by this.")

    # -- captured: production still up, and it is about to be measured -------
    if status == BaselineStatus.CAPTURED.value:
        services = len((machine.baseline or {}).get("services", []))
        if not waiting:
            return out(2, WAITING, "Production recorded",
                       f"{services} service(s) written down. Waiting for an active "
                       "campaign before anything is measured or stopped.")

        states = {c.id: canary_state(session, c, machine) for c in waiting}
        if CANARY_STOPPED in states.values():
            return out(2, BLOCKED, "Baseline canary stopped",
                       "Someone stopped it, so production is deliberately still up. "
                       "Pause and Start the campaign to try again.", canary_pending=True)
        if CANARY_EXHAUSTED in states.values():
            return out(2, BLOCKED, "Baseline canary failed",
                       "Production could not be benchmarked, so it is left running — "
                       "a machine we cannot measure is the one not to tear down. "
                       "Fix production, then Start the campaign again.", canary_pending=True)
        if CANARY_LIVE in states.values():
            return out(2, WORKING, "Benchmarking production",
                       "Measuring what production does today, before it comes down. "
                       "This is the number every candidate is compared against.",
                       canary_pending=True)
        if CANARY_DUE in states.values():
            return out(2, WORKING, "Baseline canary queued",
                       "Starts on the next tick, against the running production "
                       "service.", canary_pending=True)
        return out(3, WORKING, "Stopping production",
                   "Production is measured. Clearing it now to free the GPUs.")

    # -- cleared: the machine is ours ----------------------------------------
    if status == BaselineStatus.CLEARED.value:
        # Cleared from an EMPTY capture is a different machine than cleared by
        # us, and saying "production is down" about a box that never had any is
        # how an operator comes to expect a restore on hand-back. Split first:
        # every sentence below this point assumes we took something down.
        if nothing_was_captured(machine):
            if live:
                cards = sum(len(r.gpu_indices or []) for r in live)
                return out(3, WORKING, f"{len(live)} run(s) in flight",
                           f"Experiments hold {cards} of {machine.gpu_count} cards on a "
                           "machine that was already free — nothing of production's is "
                           "down, so nothing is owed back.")
            if waiting:
                return out(3, WORKING, "Free — nothing was running here",
                           "Capture reached the machine and found no production "
                           "services, so there was nothing to stop. All "
                           f"{machine.gpu_count} cards are available; runs start on "
                           "the next tick.")
            # DONE at Cleared, not WAITING at Restored: a hollow "Restored"
            # step is the page saying a restore is still coming, and on a
            # machine that never had production there is none to come. The
            # sequence is as far along as it will ever get here.
            return out(3, DONE, "Free — nothing was running here",
                       "Capture reached the machine and found no production services, "
                       "so nothing was stopped and nothing is owed back. Ending the "
                       "lease hands it straight back with no restore.")
        if live:
            cards = sum(len(r.gpu_indices or []) for r in live)
            return out(3, WORKING, f"{len(live)} run(s) in flight",
                       f"Experiments hold {cards} of {machine.gpu_count} cards. "
                       "Production comes back at the campaign's window end.")
        # The same question _maybe_restore asks. Asking it a second way here
        # had the page promising a restore the worker was never going to do.
        if restore_due(session, machine):
            return out(4, WORKING, "Restoring production",
                       "The window has closed and nothing is running. Production goes "
                       "back on the next tick.")
        if waiting:
            return out(3, WORKING, "Ready for experiments",
                       "Production is down and the GPUs are free. Runs start on the "
                       "next tick.")
        return out(4, WAITING, "Cleared, nothing queued",
                   "Production is down and no campaign with a window is asking for this "
                   "machine — so nothing will put it back on its own. End the lease to "
                   "hand it back; what that does to production is spelled out under "
                   "\u201cOn hand-back\u201d.")

    return out(4, DONE, status, "")


def describe_all(
    session: Session,
    default_max_run_minutes: int,
    auto: bool = True,
    auto_restore: bool = False,
) -> list[MachineLifecycle]:
    machines = session.scalars(select(Machine).order_by(Machine.id)).all()
    return [
        describe(session, m, default_max_run_minutes, auto, auto_restore)
        for m in machines
    ]
