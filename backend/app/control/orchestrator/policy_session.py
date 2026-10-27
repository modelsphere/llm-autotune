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

from app.control.launch.base import (
    DeploymentHandle,
    LaunchDeferred,
    WorkloadSpec,
    WorkloadState,
)
from app.control.launch.failures import TRANSIENT_PLACEMENT_FAILURES
from app.control.orchestrator.container_logs import clip, read_text, with_head
from app.control.orchestrator.lifecycle import as_utc, now
from app.control.orchestrator.policy_lifecycle import (
    contender_minutes,
    deadlines,
    heartbeat_silence,
    heartbeat_timeout_seconds,
    settings_of,
)
from app.control.orchestrator.schedule import from_campaign as schedule_of
from app.control.recipe.campaign import config_cards
from app.control.recipe.campaign import widest_cards as campaign_widest_cards
from app.control.search.base import CandidateConfig
from app.control.search.space import prune_inactive
from app.control.search.validation import cards_used
from app.core import apikeys
from app.db.models import (
    TERMINAL_RUN_STATES,
    TERMINAL_SESSION_STATES,
    ApiKey,
    Campaign,
    CampaignStatus,
    Candidate,
    CandidateKind,
    CandidateStatus,
    ContenderStatus,
    LlmEndpoint,
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
from app.hardware import is_node_pool

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


def redact_secrets(command: str, token: str, secrets: dict[str, str]) -> str:
    """A launch command fit for the UI and the audit trail: the session token
    and every secret env value replaced. Longest first, so a secret that
    contains another is never half-redacted."""
    out = command.replace(token, "atk_[redacted]") if token else command
    for name, value in sorted(secrets.items(), key=lambda item: -len(item[1])):
        if value:
            out = out.replace(value, f"[{name} redacted]")
    return out


def llm_endpoint_env(db, policy, *, secret: bool) -> dict[str, str]:
    """The env the policy's LLM endpoint sets: its address and model
    (secret=False), or its key (secret=True, redacted like the policy's own
    secret env). {} when the policy names no endpoint, or it was deleted."""
    if policy.llm_endpoint_id is None:
        return {}
    endpoint = db.get(LlmEndpoint, policy.llm_endpoint_id)
    if endpoint is None:
        return {}
    if secret:
        key = endpoint.api_key_value()
        return {"LLM_API_KEY": key} if key else {}
    return {"LLM_BASE_URL": endpoint.base_url.strip().rstrip("/"), "LLM_MODEL": endpoint.model}


def policy_log_path(run_log_dir: str, session_id: int) -> str:
    """Where a policy container's captured stdout+stderr lives — one file per
    session, so the capture (worker) and the reader (API) agree without a DB
    column."""
    return os.path.join(run_log_dir, f"policy-session-{session_id}.log")



def _validation_cards(
    session: PolicySession, config: dict, blockers: list, campaign: Campaign | None = None
) -> list[int] | None:
    """The cards a validation run takes: as many as its config uses
    (tp × dp × pp), all of them free within the session's block — or None
    when not enough are free yet. Validation launches run side by side, so
    a launch never borrows a card another one (or a dying engine) still
    holds; it waits for the next tick instead. A session with no card block
    (an exclusive box without slicing) takes none here: the launch driver
    places it."""
    block = list(session.gpu_indices or [])
    if not block:
        return []
    needed = max(config_cards(campaign, config) if campaign is not None else cards_used(config), 1)
    if needed > len(block):
        needed = len(block)
    held = {i for r in blockers for i in (r.gpu_indices or [])}
    free = [i for i in block if i not in held]
    if len(free) < needed:
        return None
    return sorted(free[:needed])


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

    def waiting(self, db: Session) -> list[PolicySession]:
        """Pending sessions with no machine yet — waiters in the machine queue.
        One the pending step is about to fail (no API URL, no policy row) or a
        human is aborting is not waiting for hardware."""
        if not self.sup.settings.public_api_url:
            return []
        out = []
        for row in db.scalars(
            select(PolicySession)
            .where(
                PolicySession.status == PolicySessionStatus.PENDING.value,
                PolicySession.machine_id.is_(None),
                PolicySession.abort_requested_at.is_(None),
            )
            .order_by(PolicySession.id)
        ).all():
            campaign = db.get(Campaign, row.campaign_id)
            if campaign is None or campaign.status != CampaignStatus.ACTIVE.value:
                continue
            if db.get(Policy, row.policy_id) is None:
                continue
            out.append(row)
        return out

    def try_reserve(
        self, db: Session, session: PolicySession, blocked: set[int], busy: set[int]
    ) -> bool:
        """Take the first machine this session fits and no older waiter holds,
        or report through `busy` the ones it could use once they free up."""
        campaign = db.get(Campaign, session.campaign_id)
        policy = db.get(Policy, session.policy_id)
        if campaign is None or policy is None:
            return False
        needed = settings_of(campaign).cards if campaign.share_machine else 0
        for m in self.sup._usable_machines(db, campaign):
            if is_node_pool(m) and policy.gpus_in_container:
                # Its engines run in its own pod, so all its cards must be on
                # one node; a pool hands out cards on any node.
                continue
            if m.id in blocked:
                busy.add(m.id)
                continue
            allocation = self._allocation_on(db, campaign, policy, m)
            if allocation is None:
                if (needed or 0) <= (m.gpu_count or 0):
                    busy.add(m.id)
                continue
            cards, ports = allocation
            session.machine_id = m.id
            session.gpu_indices = cards
            session.ports = ports
            session.container_name = f"autotune-policy-{session.id}"
            self.sup._event(
                db, "policy_session_reserved", campaign_id=campaign.id,
                payload={"session_id": session.id, "machine": m.name,
                         "gpus": cards, "ports": ports},
            )
            return True
        return False

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
        from app.control.orchestrator.occupancy import reservations_on

        if is_node_pool(machine):
            return self._allocation_on_pool(db, campaign, policy, machine)
        live_runs = self.sup._live_runs_on(db, machine)
        dying = self.sup._teardown_pending_on(db, machine)
        # A campaign admitted to this machine whose run is not placed yet holds
        # it as surely as that run would (orchestrator/occupancy.py).
        held = reservations_on(db, machine, exclude_campaign=campaign.id)
        n_ports = policy.ports or 1
        base = campaign.service_port or 28200
        if not campaign.share_machine:
            if machine_has_live_session(db, machine) or live_runs or held:
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
        if any(not r.share for r in held):
            return None
        held_cards = {i for t in tenants for i in (t.gpu_indices or [])}
        held_cards |= {i for r in live_runs + dying for i in (r.gpu_indices or [])}
        held_ports = {p for t in tenants for p in (t.ports or [])}
        held_ports |= {r.service_port for r in live_runs + dying if r.service_port}

        total = machine.gpu_count or 0
        needed = settings_of(campaign).cards or total
        free = [i for i in range(total) if i not in held_cards]
        free = free[: max(0, len(free) - sum(r.cards for r in held))]
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

    def _allocation_on_pool(
        self, db: Session, campaign: Campaign, policy: Policy, machine: Machine
    ) -> tuple[list[int], list[int]] | None:
        """A session's share of a node pool: as many card slots as it may use
        at once (the campaign's `cards`, else its widest config), taken next to
        whatever else runs on the pool. The slots are a budget, not devices:
        each launch's pod goes to whichever node the scheduler finds room on,
        and waits there if none has. So the only refusal is the pool's slots
        being all handed out; whether a pod fits is the cluster's answer."""
        tenants = db.scalars(
            select(PolicySession).where(
                PolicySession.machine_id == machine.id,
                PolicySession.status.not_in([s.value for s in TERMINAL_SESSION_STATES]),
            )
        ).all()
        live_runs = self.sup._live_runs_on(db, machine)
        dying = self.sup._teardown_pending_on(db, machine)
        held_cards = {i for t in tenants for i in (t.gpu_indices or [])}
        held_cards |= {i for r in live_runs + dying for i in (r.gpu_indices or [])}
        held_ports = {p for t in tenants for p in (t.ports or [])}
        held_ports |= {r.service_port for r in live_runs + dying if r.service_port}

        total = machine.gpu_count or 0
        needed = settings_of(campaign).cards or campaign_widest_cards(campaign) or 1
        free = [i for i in range(total) if i not in held_cards]
        if total and len(free) < needed:
            return None
        base = campaign.service_port or 28200
        ports = [
            p for p in range(base, base + 4096)
            if p not in held_ports and not 30000 <= p <= 32767
        ][: policy.ports or 1]
        if len(ports) < (policy.ports or 1):
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

        # The machine is the machine queue's to hand out (Supervisor._schedule
        # calls try_reserve in arrival order, alongside every campaign's next
        # run). Once it has, the reservation is durable: a launch interrupted by
        # a worker restart resumes on the SAME machine, which this still-pending
        # session now holds as a live session.
        if session.machine_id is None:
            return
        target = db.get(Machine, session.machine_id)
        if target is None:
            self._terminate(
                db, session, PolicySessionStatus.FAILED,
                "reserved machine vanished before launch", failure_class="launch",
            )
            return

        # A cold policy image pulls in the background first: the session stays
        # pending (and mints no key) until the image is on the machine.
        try:
            self.sup._driver_for(self.sup._machine_info(target)).ensure_image(
                self.sup._machine_info(target), policy.image
            )
        except LaunchDeferred as exc:
            # The policy has not started, so its message line is free to say why.
            session.policy_message = str(exc)[:2000]
            return
        except RuntimeError as exc:
            self._terminate(
                db, session, PolicySessionStatus.FAILED, str(exc)[:2000],
                failure_class="launch",
            )
            return
        session.policy_message = ""

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
        secrets = {**policy.secret_env_values(), **llm_endpoint_env(db, policy, secret=True)}
        spec = WorkloadSpec(
            name=session.container_name,
            machine=self.sup._machine_info(target),
            image=policy.image,
            env={
                # Most policies are Python, whose stdout is block-buffered when
                # it is not a terminal: what a policy prints would sit in the
                # buffer and be lost with the container, which is torn down
                # before it exits. The policy's own env can still set it.
                "PYTHONUNBUFFERED": "1",
                **(policy.env or {}),
                **llm_endpoint_env(db, policy, secret=False),
                **secrets,
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
        session.launch_command = redact_secrets(launch_command, token, secrets)
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
        self._read_vocabulary(db, campaign, target)

    def _read_vocabulary(self, db: Session, campaign: Campaign, target) -> None:
        """Read, in the background, what the platform does not know yet: the
        engine image's flags and env vars (what env keys and flags are checked
        against, GET /engine/vocabulary) and the model's own config (the
        manifest's model facts)."""
        from app.control.engines import model_facts, vocabulary_store
        from app.control.launch.base import DeploymentDriver
        from app.db.models import EngineVocabulary, ModelConfig

        try:
            if not campaign.image:
                return
            info = self.sup._machine_info(target)
            driver = self.sup._driver_for(info)
            # A substrate that cannot run a command in an image has nothing to read.
            if getattr(type(driver), "run_in_image", None) in (
                None, DeploymentDriver.run_in_image
            ):
                return
            settings = self.sup.settings
            if vocabulary_store.needs_reading(db, campaign.engine, campaign.image):
                vocabulary_store.in_background(
                    self.sup.session_factory, ("image", campaign.image),
                    lambda: EngineVocabulary(**vocabulary_store.read_image(
                        driver, info, campaign.engine, campaign.image)),
                )
            if campaign.model_path and vocabulary_store.needs_model_reading(
                db, campaign.model_path
            ):
                vocabulary_store.in_background(
                    self.sup.session_factory, ("model", campaign.model_path),
                    lambda: ModelConfig(**model_facts.read_model(
                        driver, info, campaign.image, campaign.model_path,
                        settings.engine_vocab_timeout_seconds)),
                )
        except Exception:  # noqa: BLE001 — an extra, never a reason to fail a launch
            logger.exception("could not start reading %s", campaign.image)

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
        failure = self._workload_failure(session, machine)
        if state in (WorkloadState.EXITED, WorkloadState.GONE):
            self._fail_toward_validation(
                db, session, "container_died",
                f"policy container {state.value} before its first heartbeat",
            )
        elif failure is not None and failure[0] not in TRANSIENT_PLACEMENT_FAILURES:
            # It will never start: an image that cannot be pulled, a container
            # that keeps crashing, a pod no node could ever take. Waiting out
            # the ready timeout would only hide the reason for half an hour.
            self._fail_toward_validation(
                db, session, failure[0], f"policy container cannot start: {failure[1]}"
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
        """Measure every registered contender, and production next to them,
        with the validation benchmark. Launches go side by side, as many as
        the session's free cards allow; production goes first, then the
        contenders by rank."""
        contenders = db.scalars(
            select(PolicyContender)
            .where(PolicyContender.session_id == session.id)
            .order_by(PolicyContender.rank, PolicyContender.id)
        ).all()

        # 1. Contenders being measured: resolve the ones whose run has landed.
        for contender in [c for c in contenders if c.status == ContenderStatus.SERVING.value]:
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
                continue
            if run.status not in {s.value for s in TERMINAL_RUN_STATES}:
                continue  # still measuring; the others go on
            contender.status = (
                ContenderStatus.VALIDATED.value
                if run.status == RunStatus.SUCCEEDED.value
                else ContenderStatus.FAILED.value
            )
            if run.status != RunStatus.SUCCEEDED.value and run.waiting_since is not None:
                # Ended still queued for cards: never measured, which is not a
                # verdict on the config.
                contender.status = ContenderStatus.SKIPPED.value
                contender.skip_reason = (
                    "the cluster had no free cards for it before the night ended"
                )
            if session.serving_contender_id == contender.id:
                session.serving_contender_id = None
            self.sup._event(
                db, "policy_contender_validated", campaign_id=campaign.id, run_id=run.id,
                payload={"session_id": session.id, "contender_id": contender.id,
                         "rank": contender.rank, "status": contender.status},
            )

        # 2. Launch what fits, while the clock still allows a measurement.
        reference = self._reference_run(db, session)
        want_reference = (
            settings_of(campaign).validate_production and reference is None
            and any(c.status in (ContenderStatus.REGISTERED.value, ContenderStatus.SERVING.value,
                                 ContenderStatus.VALIDATED.value) for c in contenders)
        )
        pending = [c for c in contenders if c.status == ContenderStatus.REGISTERED.value]
        if pending or want_reference:
            if self._launch_validations(db, session, campaign, machine, pending, want_reference):
                return

        # 3. Nothing left to launch: the night is over once every measurement
        # has landed. Re-read, not the list from step 1.
        if any(c.status == ContenderStatus.SERVING.value for c in contenders):
            return
        if reference is not None and reference.status not in {s.value for s in TERMINAL_RUN_STATES}:
            return
        final = (
            PolicySessionStatus.FAILED if session.failure_class else PolicySessionStatus.DONE
        )
        self._terminate(db, session, final, session.error)

    def _launch_validations(
        self, db: Session, session: PolicySession, campaign: Campaign, machine,
        pending: list, want_reference: bool,
    ) -> bool:
        """Start every validation that fits on the free cards now, production
        first. Returns True while something is still waiting to launch."""
        machine = db.get(Machine, session.machine_id) if session.machine_id else machine
        # The search engine that just finalized is still flushing GPU memory
        # and NCCL on the cards (and port) validation reuses — a container
        # holds them for ~a minute after SIGTERM. Hold the first launch until
        # the machine is confirmed clear; after that, free cards decide.
        started = any(c.run_id for c in pending) or self._reference_run(db, session) is not None
        launched_any = db.scalar(
            select(func.count(Run.id)).where(
                Run.policy_session_id == session.id, Run.kind == RunKind.EXPERIMENT.value
            )
        )
        if not started and not launched_any and machine is not None and self._teardown_blocking(
            db, session, campaign, machine
        ):
            return True
        _search, hard = deadlines(campaign, machine)
        if hard is not None and now() + timedelta(minutes=contender_minutes(campaign)) > hard:
            for contender in pending:
                contender.status = ContenderStatus.SKIPPED.value
                contender.skip_reason = "out of clock before validation"
            return False
        queue: list = (["reference"] if want_reference else []) + list(pending)
        for item in queue:
            if item == "reference":
                config = self._production_config(db, campaign, machine)
                if not self._launch_validation_run(db, session, campaign, None, config):
                    return True
                continue
            contender = item
            if self._platform_launchable(campaign, contender):
                config = dict((contender.launch_spec or {}).get("engine_args") or {})
                if not self._launch_validation_run(db, session, campaign, contender, config):
                    return True  # out of cards for now: the rest wait their turn
            elif (
                self._workload_state(db, session, machine) == WorkloadState.RUNNING
                and heartbeat_silence(session) < 2 * heartbeat_timeout_seconds(campaign)
            ):
                # Only the policy can bring this spec up, one at a time.
                if session.serving_contender_id is None:
                    contender.status = ContenderStatus.SERVING.value
                    contender.serve_commanded_at = now()
                    session.serving_contender_id = contender.id
                    self.sup._event(
                        db, "policy_contender_serve_commanded", campaign_id=campaign.id,
                        payload={"session_id": session.id, "contender_id": contender.id,
                                 "rank": contender.rank},
                    )
                return True
            else:
                contender.status = ContenderStatus.SKIPPED.value
                contender.skip_reason = (
                    "the platform cannot launch this spec and the policy is gone"
                )
        return False

    def _reference_run(self, db: Session, session: PolicySession) -> Run | None:
        """This session's validation run of the production config, if any."""
        return db.scalars(
            select(Run)
            .join(Candidate, Run.candidate_id == Candidate.id)
            .where(Run.policy_session_id == session.id, Candidate.is_baseline.is_(True))
        ).first()

    def _production_config(self, db: Session, campaign: Campaign, machine) -> dict:
        """Production as the night's reference: the recorded baseline's engine
        args for this model, engine and card type, over the campaign's base —
        the same canonical form the policy's own anchor used."""
        recorded = self.sup._campaign_baseline(db, campaign, machine)
        space = campaign.search_space or {}
        merged = {**(space.get("base") or {}), **dict(getattr(recorded, "engine_args", None) or {})}
        return prune_inactive(merged, space)

    def _platform_launchable(self, campaign: Campaign, contender) -> bool:
        """Whether the platform can bring this contender up itself — the norm.
        A spec on a different engine image (a policy that serves its own engines)
        or with no engine_args is not; only the policy can serve those."""
        spec = contender.launch_spec or {}
        image = spec.get("image") or ""
        if image and image != campaign.image:
            return False
        return bool(spec.get("engine_args"))

    def _launch_validation_run(
        self, db: Session, session: PolicySession, campaign: Campaign, contender, config: dict
    ) -> bool:
        """Bring a config up ourselves and measure it with the validation
        benchmark — the DEFAULT way a verdict is taken, launched from its own
        spec so the deciding number never depends on how a policy would have
        served it. `contender` None is production, measured as the reference
        the contenders are judged against (`is_baseline` keeps it out of the
        ranking). Returns False, changing nothing, when the session has no
        free cards or port for it yet."""
        if not config:
            if contender is not None:
                contender.status = ContenderStatus.SKIPPED.value
                contender.skip_reason = "the platform cannot launch this contender's spec"
            return True
        machine = db.get(Machine, session.machine_id)
        blockers = (
            self.sup._live_runs_on(db, machine) + self.sup._teardown_pending_on(db, machine)
            if machine is not None
            else []
        )
        cards = _validation_cards(session, config, blockers, db.get(Campaign, session.campaign_id))
        if cards is None:
            return False
        # A container holds its port for seconds after teardown is requested,
        # so walk off any port a run on this machine still holds, live or
        # tearing down. On a shared box the session's own port block keeps its
        # engines out of the neighbours' — walk that first.
        held = {r.service_port for r in blockers if r.service_port}
        port = next((p for p in (session.ports or []) if p not in held), None)
        if port is None:
            # The block is spent (it is sized for the policy's own engines);
            # any port no other session reserved will do.
            port = self.sup._free_port(
                campaign, blockers, reserved=self._ports_reserved_by_others(db, session)
            )
        candidate = Candidate(
            campaign_id=campaign.id,
            config=config,
            config_hash=(
                contender.config_hash if contender is not None
                else f"production-{CandidateConfig(engine_args=config).hash}"
            ),
            status=CandidateStatus.EXHAUSTED.value,
            kind=CandidateKind.VERIFICATION.value,
            deviations=contender.deviations if contender is not None else [],
            is_baseline=contender is None,
        )
        db.add(candidate)
        db.flush()
        run = Run(
            campaign_id=campaign.id,
            candidate_id=candidate.id,
            machine_id=session.machine_id,
            kind=RunKind.EXPERIMENT.value,  # launched + benched like any run
            status=RunStatus.PENDING.value,
            gpu_indices=cards,
            service_port=port,
            policy_session_id=session.id,
            started_at=now(),  # like the classic scheduler; no null on this path
        )
        db.add(run)
        db.flush()
        if contender is not None:
            contender.status = ContenderStatus.SERVING.value
            contender.candidate_id = candidate.id
            contender.run_id = run.id
        self.sup._event(
            db,
            "policy_contender_platform_launch" if contender is not None
            else "policy_production_validation_launch",
            campaign_id=campaign.id, run_id=run.id,
            payload={"session_id": session.id,
                     "contender_id": contender.id if contender is not None else None,
                     "gpus": cards},
        )
        return True

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

    def _workload_failure(self, session: PolicySession, machine) -> tuple[str, str] | None:
        """Why the policy container is wedged, as the substrate tells it, or
        None. Best-effort: an unreadable substrate reports nothing."""
        if machine is None or not session.container_name:
            return None
        info = self.sup._machine_info(machine)
        driver = self.sup._driver_for(info)
        handle = DeploymentHandle(
            driver=getattr(driver, "name", ""),
            container_name=session.container_name,
            machine=info,
            endpoint_url="",
            workload=True,
        )
        try:
            return driver.failure_reason(handle)
        except Exception:  # noqa: BLE001 — diagnostics never fail a session
            return None

    def _save_policy_log(self, session: PolicySession, driver, handle) -> None:
        """Persist the policy container's stdout+stderr (docker logs merges both)
        to a servable file. Best-effort: diagnostics never fail a teardown."""
        try:
            text = driver.logs(handle, tail=self.sup.settings.log_lines) or ""
        except Exception:
            logger.exception("policy log capture for session %d failed", session.id)
            return
        self._store_policy_log(session, text)

    def _store_policy_log(self, session: PolicySession, text: str) -> None:
        """Keep a read of the policy's log, replacing the one before. An empty
        read keeps what was saved before: the container may already be gone."""
        if not text.strip():
            return
        previous = read_text(policy_log_path(self.sup.settings.run_log_dir, session.id))
        text = clip(
            with_head(previous or (session.log_tail or ""), text),
            self.sup.settings.log_file_max_bytes,
        )
        # Kept in the row as well: the file is on the worker's disk, which the
        # API reads only when the two share a volume.
        session.log_tail = text[-64_000:]
        log_dir = self.sup.settings.run_log_dir
        try:
            os.makedirs(log_dir, exist_ok=True)
            with open(policy_log_path(log_dir, session.id), "w", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            # The row has it; an unwritable disk must not stop the teardown.
            logger.exception("could not write the log file of policy session %d", session.id)

    def _workload_handle(self, db: Session, session: PolicySession):
        """The driver and handle of the session's policy container, or None
        when it has no machine or container (yet)."""
        machine = db.get(Machine, session.machine_id) if session.machine_id else None
        if machine is None or not session.container_name:
            return None
        info = self.sup._machine_info(machine)
        driver = self.sup._driver_for(info)
        handle = DeploymentHandle(
            driver=getattr(driver, "name", ""),
            container_name=session.container_name,
            machine=info,
            endpoint_url="",
            workload=True,  # a policy container, not an engine service
        )
        return driver, handle

    def with_container(self, db: Session) -> list[PolicySession]:
        """Live sessions whose policy container should be running now."""
        return list(
            db.scalars(
                select(PolicySession).where(
                    PolicySession.status.in_([
                        PolicySessionStatus.STARTING.value,
                        PolicySessionStatus.SEARCHING.value,
                        PolicySessionStatus.FINALIZING.value,
                    ]),
                    PolicySession.container_name.is_not(None),
                    PolicySession.container_name != "",
                )
            ).all()
        )

    def snapshot_log(self, db: Session, session: PolicySession) -> None:
        """Save the end of a running policy's log (see container_logs.py)."""
        found = self._workload_handle(db, session)
        if found is not None:
            driver, handle = found
            self._store_policy_log(
                session, driver.logs(handle, tail=self.sup.settings.log_lines) or ""
            )

    def _capture_death_logs(self, db: Session, session: PolicySession) -> str:
        """Best-effort tail of a dead policy container's stdout, read BEFORE
        teardown's `docker rm -f` destroys it.

        For a policy that exits before its first heartbeat, this log is the only
        evidence of why (a non-retryable call, a bad env, a crash on boot); the
        platform otherwise records `container_died` with no cause. When the
        container is already gone, the last snapshot of its log stands in.
        Never raises — diagnostics are never worth failing the fail path for."""
        text = ""
        found = self._workload_handle(db, session)
        if found is not None:
            driver, handle = found
            try:
                text = driver.logs(handle, tail=self.sup.settings.log_lines) or ""
            except Exception:
                logger.exception("reading death logs for policy session %d failed", session.id)
        self._store_policy_log(session, text)
        return (session.log_tail or "").strip()

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
            # next night: the campaign is done the moment its session is. Nor
            # does a policy that said its search is exhausted: the next night
            # would start it only for it to say so again. _maybe_finish_campaign
            # never fires for policy campaigns (no candidate queue), so this is
            # where they end.
            if campaign.status == CampaignStatus.ACTIVE.value and (
                schedule_of(campaign) is None
                or session.search_end_reason == "policy_exhausted"
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
