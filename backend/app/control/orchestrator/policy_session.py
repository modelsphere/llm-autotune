"""The policy-session state machine — the worker side of the policy contract.

One session is one policy container's night on one machine:

    PENDING -> STARTING -> SEARCHING -> FINALIZING -> VALIDATING -> DONE
                                                        (FAILED / ABORTED)

Everything here runs inside the supervisor's tick, one transaction with the
rest of the fleet, and follows the same discipline as runs: a session is a row
advanced one legal step per tick, never an in-memory task. The API records
what the policy said (heartbeats, /finalized, /serving); THIS module is the
only writer of `status`.

A failed policy is not a failed night: registered contenders are validated by
fallback re-launch when the clock allows, and the session ends FAILED with
verdicts attached — the policy crashing at 04:00 loses its process, not its
results.
"""

from __future__ import annotations

import logging
import os
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.control.launch.base import DeploymentHandle, WorkloadSpec, WorkloadState
from app.control.orchestrator.lifecycle import as_utc, now
from app.control.orchestrator.policy_lifecycle import (
    deadlines,
    heartbeat_silence,
    heartbeat_timeout_seconds,
    settings_of,
)
from app.control.orchestrator.schedule import from_campaign as schedule_of
from app.core import apikeys
from app.db.models import (
    TERMINAL_RUN_STATES,
    TERMINAL_SESSION_STATES,
    ApiKey,
    BaselineStatus,
    Campaign,
    CampaignStatus,
    Candidate,
    CandidateKind,
    CandidateStatus,
    ContenderStatus,
    Machine,
    MachineState,
    Policy,
    PolicyContender,
    PolicySession,
    PolicySessionStatus,
    Run,
    RunKind,
    RunStatus,
)

logger = logging.getLogger(__name__)

MODEL_MOUNT = "/model"


def policy_volumes(campaign, policy) -> dict[str, str]:
    """What a policy container gets mounted: its campaign's extra volumes, plus
    the weights only if the policy says it reads them.

    A delegated-only policy proposes configs and lets the platform launch the
    engines, so /model is dead weight. On a cluster it is worse than dead: the
    weights are a per-node hostPath, so mounting them drags a controller that
    needs no placement and no GPU onto whichever nodes happen to carry the
    model — exactly what running it as a plain pod is meant to avoid.

    `needs_model` defaults true, so every policy registered before the field
    existed keeps the mount it was launched with. None is read as that default
    rather than as false: a column default lands at INSERT time, so a Policy
    object that has not been flushed yet carries None, and reading that as "no
    model" would drop the mount for exactly the self-serving policies that need
    it most.
    """
    volumes = dict(campaign.extra_volumes or {})
    if policy.needs_model is None or policy.needs_model:
        return {campaign.model_path: f"{MODEL_MOUNT}:ro", **volumes}
    return volumes


def policy_log_path(run_log_dir: str, session_id: int) -> str:
    """Where a policy container's captured stdout+stderr lives — one file per
    session, so the capture (worker) and the reader (API) agree without a DB
    column."""
    return os.path.join(run_log_dir, f"policy-session-{session_id}.log")


class PolicySessionEngine:
    """Advances every live policy session by one step per tick.

    Holds a reference to the Supervisor for the pieces both share — drivers,
    machine selection, run finishing, events — rather than duplicating them.
    """

    def __init__(self, supervisor) -> None:
        self.sup = supervisor

    # ------------------------------------------------------------------ tick

    def advance_all(self, db: Session) -> None:
        self._ensure_sessions(db)
        live = db.scalars(
            select(PolicySession).where(
                PolicySession.status.not_in([s.value for s in TERMINAL_SESSION_STATES])
            )
        ).all()
        for session in live:
            # One bad session must not roll back the whole tick — same rule as
            # _advance_runs.
            try:
                self._advance_one(db, session)
            except Exception:
                logger.exception("advancing policy session %d failed", session.id)

    # ------------------------------------------------------------- creation

    def _ensure_sessions(self, db: Session) -> None:
        """One PENDING session per (policy campaign, window occurrence).

        Sessions are never resurrected: the next window makes a new row, and a
        window whose session ended is a night that is over — no auto-retry, a
        crashed policy is a fact for the morning, not a loop.
        """
        campaigns = db.scalars(
            select(Campaign).where(
                Campaign.status == CampaignStatus.ACTIVE.value,
                Campaign.policy_id.is_not(None),
            )
        ).all()
        for campaign in campaigns:
            window = as_utc(campaign.window_start)
            if window is None:
                continue  # no clock, no night — activation should have refused
            existing = db.scalars(
                select(PolicySession).where(PolicySession.campaign_id == campaign.id)
            ).all()
            if any(as_utc(s.window_start) == window for s in existing):
                continue
            session = PolicySession(
                campaign_id=campaign.id,
                policy_id=campaign.policy_id,
                window_start=window,
                status=PolicySessionStatus.PENDING.value,
            )
            db.add(session)
            db.flush()
            self.sup._event(
                db, "policy_session_created", campaign_id=campaign.id,
                payload={"session_id": session.id, "window_start": window.isoformat()},
            )

    # ------------------------------------------------------------ transitions

    def _advance_one(self, db: Session, session: PolicySession) -> None:
        campaign = db.get(Campaign, session.campaign_id)
        if campaign is None:
            self._terminate(db, session, PolicySessionStatus.ABORTED, "campaign deleted")
            return
        machine = db.get(Machine, session.machine_id) if session.machine_id else None

        if session.abort_requested_at is not None and session.status in (
            PolicySessionStatus.PENDING.value,
            PolicySessionStatus.STARTING.value,
            PolicySessionStatus.SEARCHING.value,
            PolicySessionStatus.FINALIZING.value,
            PolicySessionStatus.VALIDATING.value,
        ):
            self._kill_session_runs(db, session, "session aborted")
            self._terminate(db, session, PolicySessionStatus.ABORTED, "aborted by operator")
            return

        _search, hard = deadlines(campaign, machine)
        if hard is not None and now() >= hard and session.status not in (
            PolicySessionStatus.PENDING.value,
        ):
            # The window is over. _enforce_windows kills the runs; the session
            # closes with whatever verdicts it got.
            self._kill_session_runs(db, session, "window cutoff")
            final = (
                PolicySessionStatus.FAILED
                if session.failure_class
                else PolicySessionStatus.DONE
            )
            self._terminate(db, session, final, session.error or "window cutoff")
            return

        handler = {
            PolicySessionStatus.PENDING.value: self._advance_pending,
            PolicySessionStatus.STARTING.value: self._advance_starting,
            PolicySessionStatus.SEARCHING.value: self._advance_searching,
            PolicySessionStatus.FINALIZING.value: self._advance_finalizing,
            PolicySessionStatus.VALIDATING.value: self._advance_validating,
        }.get(session.status)
        if handler is not None:
            handler(db, session, campaign, machine)

    # -- machine allocation ------------------------------------------------------

    def _allocation_on(
        self, db: Session, campaign: Campaign, policy: Policy, machine: Machine
    ) -> tuple[list[int], list[int]] | None:
        """The cards and ports this session may hold on `machine`, or None when
        it cannot be placed there right now.

        A campaign that does not share takes the whole box and only an idle
        one: every card, `policy.ports` ports from its base port. A sharing
        campaign takes a SLICE — the cards its widest candidate needs and a
        disjoint port block — and may join a box whose other tenants all
        share too (both sides opt in, as `_may_share` rules for classic runs).
        Cards and ports held by live sessions, live runs and containers still
        tearing down are off the table, so a placement never lands on hardware
        a dying engine has not released.
        """
        from app.control.orchestrator.lifecycle import machine_has_live_session

        live_runs = self.sup._live_runs_on(db, machine)
        dying = self.sup._teardown_pending_on(db, machine)
        n_ports = policy.ports or 1
        base = campaign.service_port or 28200
        if not campaign.share_machine:
            if machine_has_live_session(db, machine) or live_runs:
                return None
            return list(range(machine.gpu_count or 0)), [base + i for i in range(n_ports)]

        tenants = db.scalars(
            select(PolicySession).where(
                PolicySession.machine_id == machine.id,
                PolicySession.status.not_in([s.value for s in TERMINAL_SESSION_STATES]),
            )
        ).all()
        tenant_campaigns = {t.campaign_id for t in tenants} | {r.campaign_id for r in live_runs}
        tenant_campaigns.discard(campaign.id)
        if tenant_campaigns:
            others = db.scalars(select(Campaign).where(Campaign.id.in_(tenant_campaigns))).all()
            if not all(o.share_machine for o in others):
                return None  # someone here wanted the box to themselves
        held_cards = {i for t in tenants for i in (t.gpu_indices or [])}
        held_cards |= {i for r in live_runs + dying for i in (r.gpu_indices or [])}
        held_ports = {p for t in tenants for p in (t.ports or [])}
        held_ports |= {r.service_port for r in live_runs + dying if r.service_port}

        total = machine.gpu_count or 0
        needed = settings_of(campaign).cards or total
        free = [i for i in range(total) if i not in held_cards]
        if total and len(free) < max(1, needed):
            return None
        ports: list[int] = []
        port = base
        while len(ports) < n_ports and port < base + 4096:
            # 30000-32767 is the k8s NodePort range: kube-proxy hijacks it.
            if port not in held_ports and not 30000 <= port <= 32767:
                ports.append(port)
            port += 1
        if len(ports) < n_ports:
            return None
        return free[:needed], ports

    def _teardown_blocking(
        self, db: Session, session: PolicySession, campaign: Campaign, machine: Machine
    ) -> bool:
        """Is a dying container still on hardware THIS session is about to
        reuse? On an exclusive box any teardown counts; on a shared one only a
        container overlapping our cards or ports does — a neighbour's engine
        winding down must not stall our validation."""
        dying = self.sup._teardown_pending_on(db, machine)
        if not campaign.share_machine:
            return bool(dying)
        cards, ports = set(session.gpu_indices or []), set(session.ports or [])
        return any(
            (set(r.gpu_indices or []) & cards) or (r.service_port in ports) for r in dying
        )

    def _ports_reserved_by_others(self, db: Session, session: PolicySession) -> set[int]:
        """Ports other live sessions on this machine hold — a platform launch
        that has to leave its own block must still stay out of theirs."""
        others = db.scalars(
            select(PolicySession).where(
                PolicySession.machine_id == session.machine_id,
                PolicySession.id != session.id,
                PolicySession.status.not_in([s.value for s in TERMINAL_SESSION_STATES]),
            )
        ).all()
        return {p for o in others for p in (o.ports or [])}

    # -- PENDING: find a cleared machine and launch the container --------------

    def _advance_pending(
        self, db: Session, session: PolicySession, campaign: Campaign, machine
    ) -> None:
        settings = self.sup.settings
        if not settings.public_api_url:
            self._terminate(
                db, session, PolicySessionStatus.FAILED,
                "AUTOTUNE_PUBLIC_API_URL is not set — a policy container cannot call home",
                failure_class="config",
            )
            return
        policy = db.get(Policy, session.policy_id)
        if policy is None:
            self._terminate(
                db, session, PolicySessionStatus.FAILED, "policy row deleted",
                failure_class="config",
            )
            return

        # Reserve a machine once. A prior attempt that set machine_id but did not
        # finish launching (a worker restart mid-tick) resumes on the SAME
        # machine — re-selecting would exclude it, since this still-pending
        # session now counts as a live session on it.
        if session.machine_id is None:
            target, cards, ports = None, [], []
            for m in self.sup._usable_machines(db, campaign):
                if m.baseline_status != BaselineStatus.CLEARED.value:
                    continue
                allocation = self._allocation_on(db, campaign, policy, m)
                if allocation is not None:
                    target, cards, ports = m, *allocation
                    break
            if target is None:
                return  # baseline lifecycle still busy, or no room on a shared box
            session.machine_id = target.id
            session.gpu_indices = cards
            session.ports = ports
            session.container_name = f"autotune-policy-{session.id}"
        else:
            target = db.get(Machine, session.machine_id)
            if target is None:
                self._terminate(
                    db, session, PolicySessionStatus.FAILED,
                    "reserved machine vanished before launch", failure_class="launch",
                )
                return

        token, prefix, key_hash = apikeys.mint()
        # The key rides the MAIN transaction, alongside the session row it points
        # at — so the api_keys → policy_sessions FK is satisfied within one
        # commit (a separate transaction cannot see the not-yet-committed session
        # and fails the FK). That commit happens below, before the container is
        # launched. The plaintext exists only here (mint returns it once and we
        # never store it), so this stays in the same tick as the launch.
        db.add(
            ApiKey(
                name=f"policy-session-{session.id}",
                prefix=prefix,
                key_hash=key_hash,
                owner_id=campaign.owner_id,
                policy_session_id=session.id,
                expires_at=now() + timedelta(hours=settings.policy_token_max_hours),
            )
        )

        volumes = policy_volumes(campaign, policy)
        spec = WorkloadSpec(
            name=session.container_name,
            machine=self.sup._machine_info(target),
            image=policy.image,
            env={
                **(policy.env or {}),
                "AUTOTUNE_API_URL": settings.public_api_url,
                "AUTOTUNE_API_KEY": token,
                "AUTOTUNE_SESSION_ID": str(session.id),
            },
            volumes=volumes,
            gpu_indices=list(session.gpu_indices) if policy.gpus_in_container else None,
            ports=list(session.ports),
        )
        driver = self.sup._driver_for(spec.machine)
        # Commit the session row and its credential together, BEFORE the
        # container starts. The policy calls GET /session within a second of
        # `docker run`, from the API process; a key that rode only this tick's
        # end-of-commit is invisible to that read until the whole tick commits,
        # and a warm-cached container (the next policy on a machine) beats it
        # and gets 401 "invalid token", exiting before its first heartbeat.
        # Committing here — after the row is durable, before the side effect —
        # closes that window. Safe to commit mid-tick: every step is idempotent
        # and recomputed from the row each tick.
        db.commit()
        try:
            _handle, launch_command = driver.launch_workload(spec)
        except (RuntimeError, NotImplementedError) as exc:
            self._terminate(
                db, session, PolicySessionStatus.FAILED, str(exc)[:2000],
                failure_class="launch",
            )
            return
        # The exact command minus the credential: this lands in the UI and the
        # audit trail, and a session token in an event payload outlives the
        # session it was scoped to.
        session.launch_command = launch_command.replace(token, "atk_[redacted]")
        session.started_at = now()
        session.status = PolicySessionStatus.STARTING.value
        target.state = MachineState.RESERVED.value
        self.sup._event(
            db, "policy_session_started", campaign_id=campaign.id,
            payload={
                "session_id": session.id, "machine": target.name,
                "image": policy.image, "gpus": session.gpu_indices,
                "ports": session.ports,
            },
        )
        logger.info(
            "policy session %d launched %s on %s", session.id, policy.image, target.name
        )

    # -- STARTING: wait for the first heartbeat --------------------------------

    def _advance_starting(
        self, db: Session, session: PolicySession, campaign: Campaign, machine
    ) -> None:
        if session.first_heartbeat_at is not None:
            session.status = PolicySessionStatus.SEARCHING.value
            self.sup._event(
                db, "policy_session_searching", campaign_id=campaign.id,
                payload={"session_id": session.id,
                         "capabilities": session.capabilities,
                         "sdk_version": session.sdk_version},
            )
            return
        state = self._workload_state(db, session, machine)
        started = as_utc(session.started_at)
        waited = (now() - started).total_seconds() if started else 0.0
        if state in (WorkloadState.EXITED, WorkloadState.GONE):
            self._fail_toward_validation(
                db, session, "container_died",
                f"policy container {state.value} before its first heartbeat",
            )
        elif waited > self.sup.settings.ready_timeout_minutes * 60:
            self._fail_toward_validation(
                db, session, "ready_timeout",
                f"no heartbeat after {self.sup.settings.ready_timeout_minutes} min — "
                "is AUTOTUNE_API_URL reachable from the machine?",
            )

    # -- SEARCHING: the policy explores; we watch liveness and the clock -------

    def _advance_searching(
        self, db: Session, session: PolicySession, campaign: Campaign, machine
    ) -> None:
        state = self._workload_state(db, session, machine)
        if state in (WorkloadState.EXITED, WorkloadState.GONE):
            session.search_end_reason = "container_died"
            self._fail_toward_validation(
                db, session, "container_died", f"policy container {state.value} mid-search"
            )
            return
        silence = heartbeat_silence(session)
        timeout = heartbeat_timeout_seconds(campaign)
        if silence > 2 * timeout:
            # Alive but mute = wedged. (An API outage would mute everyone; the
            # 2x margin plus per-campaign timeout keeps a deploy from mass-
            # failing nights, per the review.)
            session.search_end_reason = "policy_wedged"
            self._fail_toward_validation(
                db, session, "policy_wedged",
                f"container alive but no heartbeat for {int(silence)}s",
            )
            return

        # Explicit lifecycle signal — the cooperative fast-path. A policy that
        # says it is done (or has given up) winds down now instead of idling to
        # the deadline. "error" also stamps the failure it self-reported.
        if session.policy_status == "exhausted":
            self._begin_finalizing(db, session, campaign, "policy_exhausted")
            return
        if session.policy_status == "error":
            session.failure_class = session.failure_class or "policy_gave_up"
            session.error = session.error or "policy reported an internal error"
            self._begin_finalizing(db, session, campaign, "policy_error")
            return

        # Deadline / explicit finalize request.
        search, _hard = deadlines(campaign, machine)
        if session.finalized_at is not None or (
            session.finalize_requested_at is not None
        ) or (search is not None and now() >= search):
            reason = "explicit" if session.finalize_requested_at is not None else "deadline"
            self._begin_finalizing(db, session, campaign, reason)
            return

        # Productivity watchdog — the enforceable backstop. A policy that is
        # alive and beating but doing no delegated work (exhausted without
        # saying so, or livelocked) earns a strike each idle window; enough
        # consecutive strikes ends the search. Any launch/benchmark request or
        # run completion resets the count, so real work — including a long
        # benchmark still in flight — never trips it.
        self._idle_watchdog(db, session, campaign)

    def _idle_watchdog(
        self, db: Session, session: PolicySession, campaign: Campaign
    ) -> None:
        knobs = settings_of(campaign)
        timeout = knobs.search_idle_timeout_s or int(
            self.sup.settings.policy_search_idle_seconds
        )
        inflight = db.scalar(
            select(func.count(Run.id)).where(
                Run.policy_session_id == session.id,
                Run.status.not_in([s.value for s in TERMINAL_RUN_STATES]),
            )
        )
        if inflight:
            return  # a launch or benchmark is running — productive by definition
        last_run_at = db.scalar(
            select(
                func.max(func.coalesce(Run.finished_at, Run.started_at, Run.created_at))
            ).where(Run.policy_session_id == session.id)
        )
        moment = now()
        reference = max(
            t
            for t in (
                as_utc(session.last_activity_at),
                as_utc(last_run_at),
                as_utc(session.started_at),
                moment - timedelta(seconds=timeout + 1),
            )
            if t is not None
        )
        if (moment - reference).total_seconds() < timeout:
            return
        # Idle past the window. Advance the strike at most once per window.
        last_strike = as_utc(session.last_idle_strike_at) or reference
        if (moment - last_strike).total_seconds() < timeout:
            return
        session.idle_strikes = (session.idle_strikes or 0) + 1
        session.last_idle_strike_at = moment
        self.sup._event(
            db, "policy_session_idle_strike", campaign_id=campaign.id,
            payload={"session_id": session.id, "strikes": session.idle_strikes,
                     "required": knobs.search_idle_strikes},
        )
        if session.idle_strikes >= knobs.search_idle_strikes:
            self._begin_finalizing(db, session, campaign, "policy_idle")

    def _begin_finalizing(
        self, db: Session, session: PolicySession, campaign: Campaign, reason: str
    ) -> None:
        """Move a searching session into FINALIZING, recording why. Idempotent
        finalize_requested_at so the grace clock starts once."""
        if session.finalize_requested_at is None:
            session.finalize_requested_at = now()
        session.search_end_reason = session.search_end_reason or reason
        session.status = PolicySessionStatus.FINALIZING.value
        self.sup._event(
            db, "policy_session_finalizing", campaign_id=campaign.id,
            payload={"session_id": session.id, "reason": reason},
        )

    # -- FINALIZING: wait for the policy to wind down, then clean the decks ----

    def _advance_finalizing(
        self, db: Session, session: PolicySession, campaign: Campaign, machine
    ) -> None:
        grace = self.sup.settings.policy_finalize_grace_seconds
        asked = as_utc(session.finalize_requested_at) or now()
        graceful = session.finalized_at is not None
        expired = (now() - asked).total_seconds() > grace
        dead = self._workload_state(db, session, machine) in (
            WorkloadState.EXITED,
            WorkloadState.GONE,
        )
        if not (graceful or expired or dead):
            return
        # Whatever the policy left running is over now: exploration ended when
        # finalize was requested, and validation needs the cards.
        self._kill_session_runs(db, session, "session finalizing")
        if dead and not graceful:
            session.failure_class = session.failure_class or "container_died"
            session.error = session.error or "policy container died before finalizing"
        session.status = PolicySessionStatus.VALIDATING.value
        self.sup._event(
            db, "policy_session_validating", campaign_id=campaign.id,
            payload={"session_id": session.id, "graceful": graceful},
        )

    # -- VALIDATING: serve/measure contenders in rank order --------------------

    def _advance_validating(
        self, db: Session, session: PolicySession, campaign: Campaign, machine
    ) -> None:
        contenders = db.scalars(
            select(PolicyContender)
            .where(PolicyContender.session_id == session.id)
            .order_by(PolicyContender.rank, PolicyContender.id)
        ).all()

        # 1. A contender being measured: wait for its run to land.
        serving = [c for c in contenders if c.status == ContenderStatus.SERVING.value]
        for contender in serving:
            run = db.get(Run, contender.run_id) if contender.run_id else None
            if run is None:
                # Commanded but the policy has not POSTed /serving yet — give it
                # the finalize grace plus a model load, then skip rather than
                # wait out the night.
                if session.serving_contender_id == contender.id:
                    commanded = as_utc(contender.serve_commanded_at) or now()
                    budget = (
                        self.sup.settings.policy_finalize_grace_seconds
                        + self.sup.settings.ready_timeout_minutes * 60
                    )
                    if (now() - commanded).total_seconds() > budget:
                        contender.status = ContenderStatus.SKIPPED.value
                        contender.skip_reason = "policy never served it"
                        session.serving_contender_id = None
                    return
                continue
            if run.status not in {s.value for s in TERMINAL_RUN_STATES}:
                return  # measurement in flight; nothing else to do this tick
            contender.status = (
                ContenderStatus.VALIDATED.value
                if run.status == RunStatus.SUCCEEDED.value
                else ContenderStatus.FAILED.value
            )
            if session.serving_contender_id == contender.id:
                session.serving_contender_id = None
            self.sup._event(
                db, "policy_contender_validated", campaign_id=campaign.id, run_id=run.id,
                payload={"session_id": session.id, "contender_id": contender.id,
                         "rank": contender.rank, "status": contender.status},
            )

        # 2. Next claim, if the clock still allows a measurement.
        pending = [c for c in contenders if c.status == ContenderStatus.REGISTERED.value]
        if pending:
            # The search engine that just finalized is still flushing GPU memory
            # and NCCL on the very cards (and port) this verify reuses — a
            # container holds them for ~a minute after SIGTERM. Launching now
            # trips the gpus_in_use / port backstop, and a sole contender then
            # ends validation with no verdict. Hold until the machine is
            # confirmed clear, the same way the classic scheduler keeps cards
            # reserved until a dying container is gone.
            if machine is not None and self._teardown_blocking(db, session, campaign, machine):
                return
            knobs = settings_of(campaign)
            _search, hard = deadlines(campaign, machine)
            nxt = pending[0]
            if hard is not None and now() + timedelta(
                minutes=knobs.approx_minutes_each + knobs.model_startup_minutes
            ) > hard:
                for contender in pending:
                    contender.status = ContenderStatus.SKIPPED.value
                    contender.skip_reason = "out of clock before validation"
                # fall through to the wrap-up below on the next tick
                return
            # The platform validates by default: it launches the contender from
            # its own spec, the same way for every campaign, so a verdict never
            # depends on how a policy would serve it. The only exception is a
            # spec the platform cannot launch (a different engine image, or no
            # engine_args) — only the policy that made it can bring that up, so
            # if it is still alive it is asked to serve; otherwise it is skipped.
            if self._platform_launchable(campaign, nxt):
                self._platform_launch(db, session, campaign, machine, nxt)
            elif (
                self._workload_state(db, session, machine) == WorkloadState.RUNNING
                and heartbeat_silence(session) < 2 * heartbeat_timeout_seconds(campaign)
            ):
                if session.serving_contender_id != nxt.id:
                    nxt.status = ContenderStatus.SERVING.value
                    nxt.serve_commanded_at = now()
                    session.serving_contender_id = nxt.id
                    self.sup._event(
                        db, "policy_contender_serve_commanded", campaign_id=campaign.id,
                        payload={"session_id": session.id, "contender_id": nxt.id,
                                 "rank": nxt.rank},
                    )
            else:
                nxt.status = ContenderStatus.SKIPPED.value
                nxt.skip_reason = "the platform cannot launch this spec and the policy is gone"
            return

        # Re-read, not the list from step 1: a contender resolved above must
        # not hold the wrap-up for one more tick.
        if any(c.status == ContenderStatus.SERVING.value for c in contenders):
            return  # a /serving we are still waiting out

        # 3. Nothing left to measure: the night is over.
        final = (
            PolicySessionStatus.FAILED if session.failure_class else PolicySessionStatus.DONE
        )
        self._terminate(db, session, final, session.error)

    def _platform_launchable(self, campaign: Campaign, contender) -> bool:
        """Whether the platform can bring this contender up itself — the norm.
        A spec on a different engine image (a policy that serves its own engines)
        or with no engine_args is not; only the policy can serve those."""
        spec = contender.launch_spec or {}
        image = spec.get("image") or ""
        if image and image != campaign.image:
            return False
        return bool(spec.get("engine_args"))

    def _platform_launch(
        self, db: Session, session: PolicySession, campaign: Campaign, machine, contender
    ) -> None:
        """Bring the contender up ourselves and measure it — the DEFAULT way a
        verdict is taken. Every campaign's contenders are validated the same
        way, launched from their own spec, so the deciding number never depends
        on how a particular policy would have served it; the policy's job ended
        when it declared the contender. Callers route only launchable specs here
        (see `_platform_launchable`); the guard below is defensive.
        """
        spec = contender.launch_spec or {}
        image = spec.get("image") or ""
        config = dict(spec.get("engine_args") or {})
        if (image and image != campaign.image) or not config:
            contender.status = ContenderStatus.SKIPPED.value
            contender.skip_reason = "the platform cannot launch this contender's spec"
            return
        candidate = Candidate(
            campaign_id=campaign.id,
            config=config,
            config_hash=contender.config_hash,
            status=CandidateStatus.EXHAUSTED.value,
            kind=CandidateKind.VERIFICATION.value,
            deviations=contender.deviations,
        )
        db.add(candidate)
        db.flush()
        # This launch fires the instant search finalizes, while the last screen
        # engine is still being torn down — and a container holds its port for
        # seconds after teardown is requested. Reusing the session's base port
        # then races that container and fails with port_conflict, sinking the
        # whole validation. So pick a port no run on this machine still holds,
        # live OR tearing down, exactly as the classic scheduler does — walking
        # off the just-freed port rather than colliding on it.
        machine = db.get(Machine, session.machine_id)
        blockers = (
            self.sup._live_runs_on(db, machine) + self.sup._teardown_pending_on(db, machine)
            if machine is not None
            else []
        )
        # On a shared box the session's port block is what keeps its engines
        # out of the neighbours' — so walk that block before the campaign-wide
        # fallback, which knows nothing about other sessions' reservations.
        held = {r.service_port for r in blockers if r.service_port}
        port = next((p for p in (session.ports or []) if p not in held), None)
        if port is None:
            port = self.sup._free_port(
                campaign, blockers, reserved=self._ports_reserved_by_others(db, session)
            )
        run = Run(
            campaign_id=campaign.id,
            candidate_id=candidate.id,
            machine_id=session.machine_id,
            kind=RunKind.EXPERIMENT.value,  # launched + benched like any run
            status=RunStatus.PENDING.value,
            gpu_indices=list(session.gpu_indices or []),
            service_port=port,
            policy_session_id=session.id,
            started_at=now(),  # like the classic scheduler; no null on this path
        )
        db.add(run)
        db.flush()
        contender.status = ContenderStatus.SERVING.value
        contender.candidate_id = candidate.id
        contender.run_id = run.id
        self.sup._event(
            db, "policy_contender_platform_launch", campaign_id=campaign.id, run_id=run.id,
            payload={"session_id": session.id, "contender_id": contender.id},
        )

    # ------------------------------------------------------------- plumbing

    def _workload_state(self, db: Session, session: PolicySession, machine) -> WorkloadState:
        if machine is None or not session.container_name:
            return WorkloadState.GONE
        info = self.sup._machine_info(machine)
        driver = self.sup._driver_for(info)
        handle = DeploymentHandle(
            driver=getattr(driver, "name", ""),
            container_name=session.container_name,
            machine=info,
            endpoint_url="",
            workload=True,  # a policy container, not an engine service
        )
        try:
            return driver.workload_state(handle)
        except (RuntimeError, NotImplementedError):
            # An ssh hiccup must not read as "container gone" — GONE fails the
            # session. Report RUNNING and let the next tick see clearly.
            return WorkloadState.RUNNING

    def _save_policy_log(self, session: PolicySession, driver, handle) -> None:
        """Persist the policy container's stdout+stderr (docker logs merges both)
        to a servable file. Best-effort: diagnostics never fail a teardown."""
        try:
            text = driver.logs(handle, tail=2000)
        except Exception:
            logger.exception("policy log capture for session %d failed", session.id)
            return
        if not text:
            return
        log_dir = self.sup.settings.run_log_dir
        os.makedirs(log_dir, exist_ok=True)
        with open(policy_log_path(log_dir, session.id), "w", encoding="utf-8") as f:
            f.write(text)

    def _capture_death_logs(self, db: Session, session: PolicySession) -> str:
        """Best-effort tail of a dead policy container's stdout, read BEFORE
        teardown's `docker rm -f` destroys it.

        For a policy that exits before its first heartbeat, this log is the only
        evidence of why (a non-retryable call, a bad env, a crash on boot); the
        platform otherwise records `container_died` with no cause. Never raises
        — diagnostics are never worth failing the fail path for."""
        machine = db.get(Machine, session.machine_id) if session.machine_id else None
        if machine is None or not session.container_name:
            return ""
        info = self.sup._machine_info(machine)
        driver = self.sup._driver_for(info)
        handle = DeploymentHandle(
            driver=getattr(driver, "name", ""),
            container_name=session.container_name,
            machine=info,
            endpoint_url="",
            workload=True,  # a policy container, not an engine service
        )
        try:
            return (driver.logs(handle, tail=200) or "").strip()
        except Exception:
            logger.exception("reading death logs for policy session %d failed", session.id)
            return ""

    def _fail_toward_validation(
        self, db: Session, session: PolicySession, failure_class: str, error: str
    ) -> None:
        """Record the failure, then still try to validate what was registered.

        The session ends FAILED either way; VALIDATING in between is what makes
        a crash at 04:00 lose the process rather than the night.
        """
        # A dead container takes its stdout with it once teardown runs. Grab the
        # tail first, while it still exists — for a policy that never
        # heartbeated, this is the whole diagnosis.
        death_logs = ""
        if failure_class == "container_died":
            death_logs = self._capture_death_logs(db, session)
            if death_logs:
                session.env_snapshot = {
                    **(session.env_snapshot or {}),
                    "death_logs": death_logs[-8000:],
                }
                error = (
                    error
                    + "\n--- last container output ---\n"
                    + "\n".join(death_logs.splitlines()[-15:])
                )
        session.failure_class = session.failure_class or failure_class
        session.error = session.error or error[:2000]
        self._kill_session_runs(db, session, f"policy {failure_class}")
        self._teardown_workload(db, session)
        session.status = PolicySessionStatus.VALIDATING.value
        self.sup._event(
            db, "policy_session_failed_validating", campaign_id=session.campaign_id,
            payload={"session_id": session.id, "failure_class": failure_class,
                     "error": error[:500],
                     "death_logs": death_logs[-2000:]},
        )
        logger.warning("policy session %d: %s (%s)", session.id, failure_class, error[:200])

    def _kill_session_runs(self, db: Session, session: PolicySession, reason: str) -> None:
        live = db.scalars(
            select(Run).where(
                Run.policy_session_id == session.id,
                Run.status.not_in([s.value for s in TERMINAL_RUN_STATES]),
            )
        ).all()
        for run in live:
            if run.llmbench_submission_id:
                self.sup.bench.cancel(run.llmbench_submission_id)
            self.sup._finish(db, run, RunStatus.KILLED, error=reason)

    def _teardown_workload(self, db: Session, session: PolicySession) -> None:
        machine = db.get(Machine, session.machine_id) if session.machine_id else None
        if machine is None or not session.container_name:
            return
        info = self.sup._machine_info(machine)
        driver = self.sup._driver_for(info)
        handle = DeploymentHandle(
            driver=getattr(driver, "name", ""),
            container_name=session.container_name,
            machine=info,
            endpoint_url="",
            workload=True,  # a policy container, not an engine service
        )
        try:
            session.env_snapshot = {
                **(session.env_snapshot or {}),
                **driver.environment(handle),
            }
        except Exception:  # provenance is never worth failing teardown for
            pass
        # The policy container's stdout+stderr, saved to a file the API can serve
        # before teardown removes the only copy — the record of what the policy
        # actually did tonight. Best-effort; never fails a teardown.
        self._save_policy_log(session, driver, handle)
        try:
            driver.teardown(handle)
        except Exception:
            logger.exception(
                "teardown of %s failed (janitor will catch it)", session.container_name
            )

    def _terminate(
        self,
        db: Session,
        session: PolicySession,
        status: PolicySessionStatus,
        error: str = "",
        failure_class: str = "",
    ) -> None:
        self._teardown_workload(db, session)
        if failure_class and not session.failure_class:
            session.failure_class = failure_class
        if error and not session.error:
            session.error = error[:2000]
        session.status = status.value

        # The token dies with the session, plus the grace the contract promises
        # (a policy that gets 410 on its next heartbeat may still want to read).
        key = db.scalars(
            select(ApiKey).where(ApiKey.policy_session_id == session.id)
        ).first()
        if key is not None:
            grace = now() + timedelta(hours=self.sup.settings.policy_token_grace_hours)
            key.expires_at = min(as_utc(key.expires_at) or grace, grace)

        machine = db.get(Machine, session.machine_id) if session.machine_id else None
        if machine is not None:
            self.sup._release_machine_if_idle(db, machine)

        campaign = db.get(Campaign, session.campaign_id)
        if campaign is not None:
            self.sup._event(
                db, "policy_session_ended", campaign_id=campaign.id,
                payload={"session_id": session.id, "status": session.status,
                         "failure_class": session.failure_class,
                         "validated": self._verdict_count(db, session)},
            )
            # A one-off window (force-start, or no recurring schedule) has no
            # next night: the campaign is done the moment its session is.
            # _maybe_finish_campaign never fires for policy campaigns (no
            # candidate queue), so this is where they end.
            if (
                schedule_of(campaign) is None
                and campaign.status == CampaignStatus.ACTIVE.value
            ):
                campaign.status = CampaignStatus.DONE.value
                self.sup._event(db, "campaign_done", campaign_id=campaign.id)
        logger.info("policy session %d ended: %s", session.id, session.status)

    def _verdict_count(self, db: Session, session: PolicySession) -> int:
        return len(
            db.scalars(
                select(PolicyContender).where(
                    PolicyContender.session_id == session.id,
                    PolicyContender.status == ContenderStatus.VALIDATED.value,
                )
            ).all()
        )
