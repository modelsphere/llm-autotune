"""The supervisor: a tick loop that advances campaigns and the run state
machine. The only component with authority to spend resources.

Each tick (non-blocking per run):
  1. reconcile     re-attach to non-terminal runs after a restart
  2. plan          expand candidates for active campaigns (space + validator)
  3. schedule      start runs while machines are free and the window allows
  4. advance       move each live run one step through its state machine
  5. enforce       hard-kill anything past the campaign window end
"""

import logging
import os
import traceback
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.control.baseline import resolve_baseline, same_config
from app.control.engine_command import baseline_engine_args, engine_of_command
from app.control.launch import (
    DeploymentDriver,
    DeploymentState,
    LaunchSpec,
    MachineInfo,
    NodeAssignment,
    get_driver,
    merge_volumes,
)
from app.control.launch.failures import (
    RELEASED,
    classify_exit,
    classify_failure,
    is_infrastructure,
    is_transient_placement,
)
from app.control.orchestrator import schedule as sched
from app.control.orchestrator.groups import members_by_rank
from app.control.orchestrator.lifecycle import (
    CANARY_DUE,
    USER_STOP_ERROR,
    accepts_new_work,
    campaigns_using,
    campaigns_waiting_on,
    canaries_since_activation,
    canary_state,
    drain_kills_running,
    frozen_by_pause,
    has_valid_candidates,
    last_activation,
    last_cleared_at,
    lease_expired,
    live_runs_on,
    machine_has_live_session,
    restore_due,
    teardown_pending_on,
    window_allows_new_run,
    window_allows_run_of,
)
from app.control.orchestrator.lifecycle import (
    as_utc as _as_utc,
)
from app.control.orchestrator.lifecycle import (
    now as _now,
)
from app.control.orchestrator.packing import Placement, choose_next, free_indices
from app.control.orchestrator.states import can_transition
from app.control.promotion import winner as winner_of
from app.control.run_nodes import machine_hosts_live_gang, nodes_of, run_ids_on_machine
from app.control.search import CandidateConfig
from app.control.search.space import expand
from app.control.search.validation import (
    ValidationContext,
    validate_config,
)
from app.control.search.validation import (
    cards_used as _cards_used,
)
from app.core.config import get_settings
from app.datasets import pinning
from app.datasets.profiles import (
    BuildInFlight,
    DatasetBuild,
    DatasetProfileClient,
    DatasetProfileError,
)
from app.db.models import (
    TERMINAL_RUN_STATES,
    TERMINAL_SESSION_STATES,
    Baseline,
    BaselineStatus,
    Campaign,
    CampaignStatus,
    Candidate,
    CandidateKind,
    CandidateStatus,
    Event,
    LeaseEndMode,
    LeaseState,
    Machine,
    MachineGroup,
    MachineState,
    PolicySession,
    PolicyTrial,
    Promotion,
    Result,
    Run,
    RunKind,
    RunNode,
    RunStatus,
    User,
    is_baseline_candidate,
    is_in_place_baseline,
)
from app.evaluation import (
    EvalStatus,
    Evaluator,
    EvaluatorBusy,
    EvaluatorRejected,
    HealthEvaluator,
    LLMBenchEvaluator,
    summarize,
)
from app.evaluation.aggregate import summary_of
from app.objective import sort_key
from app.staging import (
    SCREEN,
    VERIFY,
    is_staged,
    stage_of,
    stage_of_run,
)
from app.staging import (
    benchmark_slug as _benchmark_slug,
)
from app.staging import (
    max_run_minutes as _stage_max_run_minutes,
)
from app.staging import (
    objective as _stage_objective,
)

logger = logging.getLogger(__name__)


# Which failures are the config's fault and which are the infrastructure's now
# lives in app.control.launch.failures — the search needs the same distinction
# to know which failed points are real observations, and it cannot import an
# orchestrator private.
#
# Deliberately NOT retried: `supervisor_error` means our own code raised
# something unanticipated, which is nearly always deterministic. Retrying it
# costs a container launch, a model load and a full benchmark per attempt —
# roughly 50 minutes of a night spent re-hitting the same bug. It stays failed
# and visible; a human can re-queue it once the bug is fixed.
MAX_ATTEMPTS_PER_CANDIDATE = 3

# How many configs to draw from a search whose proposals do not depend on
# results (a grid). Bounds one tick's work on a very large space; the rest
# arrive on later ticks because the search skips what it has already seen.
DEFAULT_PROPOSAL_BATCH = 100

# Where a multi-node run's rendezvous port is searched from, on the master. Above
# the usual service ports and outside the k8s NodePort range; the allocator walks
# up from here until it finds one free on the master.
DIST_PORT_BASE = 29500


def _crash_detail(exc: BaseException, frames: int = 4) -> str:
    """A self-contained explanation of a supervisor crash.

    "unexpected supervisor exception" told an operator nothing and sent them
    hunting through the worker log over ssh. The exception and the last few
    frames of OUR code are what actually locate the bug.
    """
    summary = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    tb = traceback.extract_tb(exc.__traceback__)
    tail = [
        f"{frame.filename.rsplit('/', 1)[-1]}:{frame.lineno} in {frame.name}"
        for frame in tb[-frames:]
    ]
    where = " <- ".join(reversed(tail))
    return f"supervisor crashed: {summary}" + (f" [{where}]" if where else "")


def _bench_failure_class(outcome) -> str:
    """Separate "the benchmark could not run" from "the service ran and failed
    it". The second is a verdict on the config — a redline breach or a metric
    threshold — and the search should treat it as such, not as flaky infra."""
    if (outcome.raw or {}).get("passed") is False:
        return "benchmark_not_passed"
    return "benchmark"


# Health errors that mean "nothing is listening" rather than "it answered
# wrongly". The distinction decides whether to wait or to condemn the service:
# a restored production container is unreachable for minutes while it loads.
_UNREACHABLE_MARKERS = (
    "connecterror",
    "connecttimeout",
    "connection refused",
    "connect call failed",
    "readtimeout",
    "all connection attempts failed",
)


def _looks_unreachable(error: str | None) -> bool:
    lowered = (error or "").lower()
    return any(marker in lowered for marker in _UNREACHABLE_MARKERS)


def _parse_rfc3339(value) -> datetime | None:
    """A k8s timestamp ("2026-08-17T06:15:00Z") as an aware UTC datetime, or
    None. Tolerant: a field the substrate did not set must not raise on the
    readiness path."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _elapsed_seconds(run: Run) -> float | None:
    """Wall-clock a finished run took, launch to verdict.

    Cost, in the only currency a night has. A policy that knows a config
    takes 90 minutes can weigh it against one that takes 20 — and an
    unfinished run has no duration rather than a duration of zero.
    """
    started, finished = _as_utc(run.started_at), _as_utc(run.finished_at)
    if started is None or finished is None:
        return None
    return max(0.0, (finished - started).total_seconds())


class Supervisor:
    def __init__(
        self,
        session_factory,
        driver_name: str = "ssh_docker",
        health_evaluator: Evaluator | None = None,
        bench_evaluator: Evaluator | None = None,
        dataset_profiles: DatasetProfileClient | None = None,
    ):
        self.session_factory = session_factory
        # The platform default driver. A machine may name a different substrate
        # (ssh_docker vs k8s) — `_driver_for` resolves per machine and caches
        # each one — but a machine that names nothing, and every test that
        # injects a driver by setting `self.driver`, uses this one. The cache key
        # is (substrate, cluster): two k8s clusters are two drivers.
        self.driver = get_driver(driver_name)
        self._drivers: dict[tuple[str, int | None], DeploymentDriver] = {}
        self.health = health_evaluator or HealthEvaluator(
            timeout_seconds=get_settings().health_probe_timeout_seconds
        )
        self.bench = bench_evaluator or LLMBenchEvaluator()
        # Built lazily: a platform with no pinned campaigns never calls it, and
        # constructing one reaches for LLMBench credentials.
        self._dataset_profiles = dataset_profiles
        self.settings = get_settings()
        self._policy_engine = None

    @property
    def datasets(self) -> DatasetProfileClient:
        if self._dataset_profiles is None:
            self._dataset_profiles = DatasetProfileClient()
        return self._dataset_profiles

    # ------------------------------------------------------------------ tick

    def tick(self) -> None:
        with self.session_factory() as session:
            # Clocks first. Both of these decide whether work may start at all
            # this tick — a campaign whose window just opened, a machine whose
            # lease just ended — and running them after planning would spend a
            # tick placing runs the very next step tears down.
            self._advance_campaign_schedules(session)
            self._advance_leases(session)

            self._process_stop_requests(session)
            # Before planning: a paused campaign stops queueing for cluster
            # cards, and the candidates it gives back are pending again in time
            # for this same tick to consider them if it is resumed.
            self._release_queued_while_paused(session)
            self._advance_dataset_pins(session)
            self._plan(session)
            self._advance_baseline_lifecycle(session)
            # After the baseline lifecycle (a session needs a CLEARED machine),
            # before scheduling (its machine must be reserved before _schedule
            # could offer it to a classic campaign).
            self._advance_policy_sessions(session)
            self._schedule(session)
            self._advance_runs(session)
            self._enforce_windows(session)
            # Last: confirm the containers finished this tick are actually gone
            # (freeing their machines) and escalate any that will not die. Runs
            # after teardown so a run killed this tick is reaped the same tick
            # when its container goes quietly — matching the old immediate
            # release — and lingers only when it genuinely refuses to.
            self._reap_teardowns(session)
            # Last of all, and deliberately after everything that can finish a
            # campaign: a campaign that asked to propose its own winner does it
            # in the same tick it becomes DONE.
            self._advance_auto_promotions(session)
            session.commit()

    # ---------------------------------------------------- unattended promotion

    def _advance_auto_promotions(self, session: Session) -> None:
        """Propose the winner of a finished campaign that asked for it.

        Keyed on the campaign being DONE rather than hooked into the places
        that set it — a campaign reaches DONE from a closed schedule, an
        exhausted search and a policy session ending, and a proposal that only
        happened down some of those paths would be worse than none.

        Runs at most once per campaign: the existence of ANY promotion row is
        the mark. A failed one stays failed and visible on the campaign page
        rather than being retried every ten seconds — and a winner promoted by
        hand first means the platform has nothing left to say.

        When there is nothing to propose — no successful run, a winner that
        crosses a redline, a winner production already runs — it says so once
        and keeps the flag ON. DONE is not final: retrying the failed runs of a
        campaign whose every launch was refused revives it, and a campaign that
        disarmed itself on the way past would never propose the winner it went
        on to find. "Once" is one `auto_promotion_skipped` event per reason; a
        DIFFERENT reason is news and is recorded again.
        """
        campaigns = session.scalars(
            select(Campaign).where(
                Campaign.status == CampaignStatus.DONE.value,
                Campaign.auto_promote.is_(True),
            )
        ).all()
        if not campaigns:
            return
        promoted = set(
            session.scalars(
                select(Promotion.campaign_id).where(
                    Promotion.campaign_id.in_([c.id for c in campaigns])
                )
            ).all()
        )
        target_name = self.settings.promotion_target or "manual"
        for campaign in campaigns:
            if campaign.id in promoted:
                continue
            try:
                found = winner_of.resolve(session, campaign)
            except Exception:  # noqa: BLE001 — one campaign must not stop the pass
                logger.exception("auto-promotion could not resolve campaign %d", campaign.id)
                continue
            if found is None:
                self._skip_promotion(
                    session, campaign, "no successful, benchmarked run to promote"
                )
                continue
            if not found.holds_redlines:
                # A winner that crosses an SLO is a rejected option. The button
                # can force it past this; an unattended pass must not.
                self._skip_promotion(
                    session, campaign, "the best run crosses a redline",
                    run_id=found.run.id, breaches=list(found.entry.breaches or []),
                )
                continue
            draft = None
            if target_name == "gitlab":
                try:
                    draft = winner_of.draft_for(
                        found, actor="autotune", ui_url=self.settings.public_ui_url
                    )
                except Exception as exc:  # noqa: BLE001 — recorded, not raised
                    logger.exception("auto-promotion could not draft campaign %d", campaign.id)
                    draft = winner_of.failed_draft(str(exc))
                if draft.unchanged:
                    # The winner IS what production runs. Nothing to propose,
                    # and nothing went wrong — a promotion row here would be a
                    # red badge on a campaign that did its job.
                    self._skip_promotion(
                        session, campaign, draft.reason, run_id=found.run.id
                    )
                    continue
            promotion = winner_of.promote(
                session,
                found,
                actor="autotune",
                target_name=target_name,
                ui_url=self.settings.public_ui_url,
                draft=draft,
            )
            self._event(
                session, "promotion_opened", campaign_id=campaign.id, run_id=found.run.id,
                payload={
                    "promotion": promotion.id,
                    "target": promotion.target,
                    "state": promotion.state,
                    "auto": True,
                    "branch": (promotion.refs or {}).get("target_branch", ""),
                    "mr_url": (promotion.refs or {}).get("mr_url", ""),
                    "error": promotion.error,
                },
            )
            logger.info(
                "campaign %d proposed its winner (run %d) to %s: %s",
                campaign.id, found.run.id, promotion.target, promotion.state,
            )

    def _skip_promotion(
        self, session: Session, campaign: Campaign, reason: str, **payload
    ) -> None:
        """Record that there is nothing to propose — once per reason.

        The flag stays on: a campaign that reaches DONE with nothing to say may
        still gain a winner (retried runs, a reopened window), and one that
        disarmed itself on the way past could never propose it.
        """
        last = session.scalars(
            select(Event)
            .where(Event.campaign_id == campaign.id, Event.kind == "auto_promotion_skipped")
            .order_by(Event.id.desc())
            .limit(1)
        ).first()
        if last is not None and (last.payload or {}).get("reason") == reason:
            return
        self._event(
            session, "auto_promotion_skipped", campaign_id=campaign.id,
            payload={"reason": reason, **payload},
        )

    # ------------------------------------------------------- campaign clocks

    def _advance_campaign_schedules(self, session: Session) -> None:
        """Wake and stand down campaigns that carry a nightly window.

        The campaign's own status is the only thing that changes here; the
        window it is currently serving is written onto `window_start/end` so
        every other part of the loop keeps reading two plain datetimes and
        never learns that a schedule exists.

        PAUSED is untouched on purpose. A human holding a campaign outranks the
        clock — otherwise pausing at 23:30 would be undone on the next tick,
        and there would be no way to stop a nightly job short of deleting it.
        """
        campaigns = session.scalars(
            select(Campaign).where(
                Campaign.status.in_(
                    [CampaignStatus.SCHEDULED.value, CampaignStatus.ACTIVE.value]
                )
            )
        ).all()
        for campaign in campaigns:
            # A live override outranks the clock in both directions: it keeps a
            # force-started campaign awake outside its window, and it is what
            # makes Force start stick at all — without it the very next tick
            # would see no open window and put the campaign straight back to
            # sleep.
            override = _as_utc(campaign.override_until)
            if override is not None:
                if _now() < override:
                    continue
                campaign.override_until = None
                self._event(session, "campaign_override_expired", campaign_id=campaign.id)

            schedule = sched.from_campaign(campaign)
            if schedule is None:
                continue  # a one-off window, or none; nothing to advance
            window = sched.current_window(_now(), schedule)

            if window is not None:
                start, end = window
                # Rewritten every tick, not just at the transition: editing the
                # schedule of a running campaign has to take effect tonight,
                # and the occurrence is cheap to recompute.
                campaign.window_start, campaign.window_end = start, end
                if campaign.status != CampaignStatus.ACTIVE.value:
                    campaign.status = CampaignStatus.ACTIVE.value
                    self._event(
                        session, "campaign_window_opened", campaign_id=campaign.id,
                        payload={"window_start": start.isoformat(),
                                 "window_end": end.isoformat()},
                    )
                    logger.info("campaign %d woke for its window", campaign.id)
                continue

            if campaign.status != CampaignStatus.ACTIVE.value:
                continue  # already standing by

            # The window closed. Live runs are cut by _enforce_windows against
            # the same window_end, so nothing is left holding a machine.
            if sched.is_finished(_now(), schedule):
                campaign.status = CampaignStatus.DONE.value
                self._event(session, "campaign_schedule_finished", campaign_id=campaign.id)
                logger.info("campaign %d has no windows left; done", campaign.id)
            else:
                campaign.status = CampaignStatus.SCHEDULED.value
                self._event(session, "campaign_window_closed", campaign_id=campaign.id)
                logger.info("campaign %d stood down until its next window", campaign.id)

    # ------------------------------------------------------- dataset pinning

    def _advance_dataset_pins(self, session: Session) -> None:
        """Settle which dataset build each active campaign will be measured on.

        Deliberately early, and deliberately not where it is needed. Screening
        48 candidates takes hours and never touches the dataset, so a build
        asked for when the window opens is published long before the first
        verification run wants it. Nothing here blocks a tick: a build in
        flight is a row id to poll next time round.

        Only campaigns that have not pinned yet and have not already concluded
        the profile cannot produce one — otherwise a refused build would be
        re-requested every tick for the rest of the night.
        """
        campaigns = session.scalars(
            select(Campaign).where(
                Campaign.status == CampaignStatus.ACTIVE.value,
                Campaign.dataset_profile != "",
                Campaign.dataset_build_id == "",
                Campaign.dataset_policy_applied == "",
            )
        ).all()
        for campaign in campaigns:
            try:
                if campaign.dataset_build_row:
                    self._poll_dataset_build(session, campaign)
                else:
                    self._choose_dataset_build(session, campaign)
            except DatasetProfileError as exc:
                # The platform is unreachable or unhappy. Not fatal and not
                # concluded: screening carries on and the next tick asks again.
                logger.warning(
                    "campaign %d: dataset profile %s: %s",
                    campaign.id, campaign.dataset_profile, exc,
                )

    def _choose_dataset_build(self, session: Session, campaign: Campaign) -> None:
        profile = self.datasets.find(campaign.dataset_profile)
        if profile is None:
            self._conclude_no_dataset(
                session, campaign, f"no profile named '{campaign.dataset_profile}'"
            )
            return

        current = self.datasets.current(profile)
        held = (
            pinning.holders(session, campaign.dataset_profile, current.build_id)
            if current
            else []
        )
        # This campaign is in the query because it has no pin, so it cannot be
        # holding anything; no need to exclude itself from `held`.
        decision = pinning.decide(pinning.policy_of(campaign), current, held)

        if decision == pinning.DECIDE_ADOPT:
            self._pin_dataset(
                session, campaign, current, pinning.APPLIED_ADOPTED,
                held_by=[c.id for c in held],
            )
            return
        if decision == pinning.DECIDE_TAKE_CURRENT:
            self._pin_dataset(session, campaign, current, pinning.APPLIED_CURRENT)
            return

        try:
            row = self.datasets.trigger_build(int(profile["id"]))
        except BuildInFlight:
            # Someone asked first. Poll THEIR row rather than asking again
            # every tick and then triggering a second build the moment the
            # first one publishes.
            row = self.datasets.latest_build_row(int(profile["id"]))
            if not row:
                return  # next tick; nothing recorded, nothing concluded
        campaign.dataset_build_row = row
        self._event(
            session, "dataset_build_requested", campaign_id=campaign.id,
            payload={"profile": campaign.dataset_profile, "build_row": row},
        )
        logger.info(
            "campaign %d asked %s for a fresh build (row %d)",
            campaign.id, campaign.dataset_profile, row,
        )

    def _poll_dataset_build(self, session: Session, campaign: Campaign) -> None:
        build = self.datasets.build(campaign.dataset_build_row)
        status = build.get("status") or ""
        if status in ("pending", "running"):
            return

        row = campaign.dataset_build_row
        campaign.dataset_build_row = 0
        if status == "ready":
            published = DatasetBuild.from_api(build)
            if published is not None:
                self._pin_dataset(session, campaign, published, pinning.APPLIED_REBUILT)
                return

        # A failed build leaves the previous one serving, so there may still be
        # something to measure against — older than asked for, but consistent,
        # which is the property that actually matters.
        self._event(
            session, "dataset_build_failed", campaign_id=campaign.id,
            payload={"build_row": row, "status": status, "error": build.get("error") or ""},
        )
        logger.warning(
            "campaign %d: build %d ended %s (%s)",
            campaign.id, row, status, build.get("error") or "no reason given",
        )
        profile = self.datasets.find(campaign.dataset_profile)
        current = self.datasets.current(profile) if profile else None
        if current is not None:
            self._pin_dataset(session, campaign, current, pinning.APPLIED_CURRENT)
            return
        self._conclude_no_dataset(
            session, campaign, build.get("error") or f"build {row} {status}"
        )

    def _pin_dataset(
        self,
        session: Session,
        campaign: Campaign,
        build: DatasetBuild,
        applied: str,
        held_by: list[int] | None = None,
    ) -> None:
        campaign.dataset_build_id = build.build_id
        campaign.dataset_sha256 = build.sha256
        campaign.dataset_pinned_at = _now()
        campaign.dataset_policy_applied = applied
        campaign.dataset_build_row = 0
        self._event(
            session, "dataset_pinned", campaign_id=campaign.id,
            payload={
                "profile": campaign.dataset_profile,
                "build_id": build.build_id,
                "sha256": build.sha256,
                "records": build.records,
                "window": [build.window_start, build.window_end],
                "requested": pinning.policy_of(campaign),
                "applied": applied,
                "held_by": held_by or [],
            },
        )
        logger.info(
            "campaign %d pinned %s/%s (%d records, %s)",
            campaign.id, campaign.dataset_profile, build.build_id, build.records, applied,
        )

    def _conclude_no_dataset(self, session: Session, campaign: Campaign, reason: str) -> None:
        """Stop asking. The campaign finishes on its screening evidence.

        Recorded rather than retried: the failures that reach here are refusals
        with a reason — a profile that does not exist, a collector that will
        not build from too few records — and none of them resolve by asking
        again in thirty seconds.
        """
        campaign.dataset_policy_applied = pinning.APPLIED_UNAVAILABLE
        campaign.dataset_build_row = 0
        self._event(
            session, "dataset_unavailable", campaign_id=campaign.id,
            payload={"profile": campaign.dataset_profile, "reason": reason},
        )
        logger.error(
            "campaign %d: no dataset from %s (%s); verification will be skipped",
            campaign.id, campaign.dataset_profile, reason,
        )

    # -------------------------------------------------------- machine leases

    def _advance_leases(self, session: Session) -> None:
        """Carry a hand-back request through to production being back up.

        Ending a lease is not an instant: runs have to stop, and production has
        to be restored before the machine is any use to whoever asked for it.
        The steps are the same for both modes; only whether live runs are
        waited for or killed differs.
        """
        for machine in session.scalars(select(Machine)).all():
            try:
                if lease_expired(machine) and not frozen_by_pause(session, machine):
                    # Nobody asked, but we said we would hand it back by now.
                    # Unless a human paused the campaign holding it: pause means
                    # frozen, so the lease clock stops and the machine is left
                    # untouched until they resume or end the lease themselves.
                    self._begin_drain(session, machine, LeaseEndMode.POLITE.value,
                                      actor="worker", reason="lease_due")
                if machine.lease_state != LeaseState.DRAINING.value:
                    continue
                self._advance_drain(session, machine)
            except Exception:
                logger.exception("lease handling for %s failed", machine.name)

    def _begin_drain(
        self, session: Session, machine: Machine, mode: str, actor: str, reason: str = ""
    ) -> None:
        machine.lease_state = LeaseState.DRAINING.value
        machine.lease_end_mode = mode
        self._event(
            session, "lease_end_requested",
            payload={"machine": machine.name, "mode": mode, "reason": reason, "by": actor},
        )
        logger.info("lease on %s is ending (%s, %s)", machine.name, mode, reason or actor)

    def _advance_drain(self, session: Session, machine: Machine) -> None:
        # A live policy session drains like a run does: polite = tell it to
        # finalize (its contenders still get validated inside what remains of
        # the clock), eager/deadline = abort it now. Either way the machine is
        # not released while the container stands.
        live_session = session.scalars(
            select(PolicySession).where(
                PolicySession.machine_id == machine.id,
                PolicySession.status.not_in([s.value for s in TERMINAL_SESSION_STATES]),
            )
        ).first()
        if live_session is not None:
            if drain_kills_running(machine):
                if live_session.abort_requested_at is None:
                    live_session.abort_requested_at = _now()
                    self._event(
                        session, "policy_session_abort_requested",
                        campaign_id=live_session.campaign_id,
                        payload={"session_id": live_session.id, "reason": "lease ended"},
                    )
            elif live_session.finalize_requested_at is None:
                live_session.finalize_requested_at = _now()
                self._event(
                    session, "policy_session_finalize_requested",
                    campaign_id=live_session.campaign_id,
                    payload={"session_id": live_session.id, "reason": "lease draining"},
                )
            return

        live = session.scalars(
            select(Run).where(
                # Any node of a run, not just the master: draining a machine
                # that is a gang's worker must still stop the gang.
                Run.id.in_(run_ids_on_machine(machine.id)),
                Run.status.not_in([s.value for s in TERMINAL_RUN_STATES]),
            )
        ).all()

        if live:
            if not drain_kills_running(machine):
                return  # polite: let them finish, start nothing new
            for run in live:
                if run.llmbench_submission_id:
                    self.bench.cancel(run.llmbench_submission_id)
                self._finish(session, run, RunStatus.KILLED, error="lease ended")
            return  # release on the next tick, once teardown has settled

        # Nothing is running. If we are configured to put production back it
        # goes back before the machine does — handing over a box whose service
        # is still down is the outcome the capture/restore mechanism exists to
        # prevent. With auto-restore off (the default) the admin owns that
        # put-back: we hand the box back with production exactly as we left it,
        # flag the manual restore now owed, and drop our baseline claim so the
        # next hand-over re-inspects the box rather than trusting a stale one.
        if machine.baseline_status == BaselineStatus.CLEARED.value:
            services = (machine.baseline or {}).get("services")
            if services and self.settings.auto_restore_production:
                self._restore_production(session, machine, trigger="lease_end")
                return  # confirm the restore landed before closing the lease
            if services:
                self._event(
                    session, "production_left_down",
                    payload={"machine": machine.name,
                             "services": [s.get("container") for s in services]},
                )
                machine.baseline_status = BaselineStatus.NONE.value
            else:
                machine.baseline_status = BaselineStatus.RESTORED.value

        machine.lease_state = LeaseState.RELEASED.value
        machine.lease_released_at = _now()
        machine.state = MachineState.AWAY.value
        self._event(
            session, "lease_released",
            payload={"machine": machine.name, "mode": machine.lease_end_mode,
                     "baseline_status": machine.baseline_status},
        )
        logger.info("lease on %s released; machine handed back", machine.name)

    # -------------------------------------------------- baseline lifecycle

    def _advance_baseline_lifecycle(self, session: Session) -> None:
        """Drive capture -> cleared -> restored without a human in the loop.

        A night is unattended by definition, so the platform must be able to
        take production down and put it back on its own. The safety does not
        come from someone being awake, it comes from:
          - the machine being marked available (that IS the hand-over),
          - a capture existing (teardown is reversible), and
          - a canary having PASSED (the machine is healthy and measured).
        A failed canary leaves production untouched — a suspect machine is
        exactly the one not to clear.
        """
        if not self.settings.auto_baseline_lifecycle:
            return
        for machine in session.scalars(select(Machine)).all():
            if machine.state == MachineState.RESERVED.value:
                continue  # a run holds it; decide later
            if machine.lease_state == LeaseState.DRAINING.value:
                continue  # _advance_drain owns this machine until it is handed back
            try:
                if machine.baseline_status in (
                    BaselineStatus.NONE.value,
                    BaselineStatus.RESTORED.value,
                ):
                    self._maybe_capture(session, machine)
                elif machine.baseline_status == BaselineStatus.CAPTURED.value:
                    self._maybe_clear(session, machine)
                elif machine.baseline_status == BaselineStatus.CLEARED.value:
                    self._maybe_restore(session, machine)
            except Exception:
                logger.exception("baseline lifecycle for %s failed", machine.name)

    def _maybe_capture(self, session: Session, machine: Machine) -> None:
        """Take the inventory that makes teardown reversible, on our own.

        Capture only reads — it inspects what is running and writes it down —
        so the preconditions are just that the machine has been handed over
        (state available) and that some campaign is actually waiting for it.

        Requiring a human to press Capture meant only the FIRST night was
        unattended: a night ends with production RESTORED, and nothing moved a
        machine from there back to CAPTURED, so the next campaign sat behind a
        banner until someone clicked. Automating clear-and-restore while
        leaving capture manual automated every step except the one that starts
        the sequence.
        """
        if machine.state != MachineState.AVAILABLE.value:
            return  # away = not ours yet; the hand-over itself stays manual
        if not self._campaigns_waiting_on(session, machine):
            return  # nobody needs this machine; do not touch it

        info = self._machine_info(machine)
        baseline = self._driver_for(info).capture_baseline(info)
        services = baseline.get("services", [])
        prior = (machine.baseline or {}).get("services")
        if not services and prior:
            # Prod-priority safeguard. We captured production on this box before
            # and now see nothing. A service restarting is far likelier than the
            # box having quietly become ours — and overwriting the capture with
            # an empty one would both strand production (nothing left to restore
            # it with) and mark the machine free, clearing the way to run our own
            # containers over it. Keep the existing capture and status; the empty
            # reading is treated as transient and retried on the next tick, never
            # as permission to discard how production comes back.
            logger.warning(
                "capture on %s found nothing but a prior baseline exists "
                "(%d service(s)); keeping it — production may be restarting",
                machine.name, len(prior),
            )
            self._event(
                session, "baseline_capture_kept_prior",
                payload={"machine": machine.name,
                         "prior_services": [s.get("container") for s in prior]},
            )
            return
        machine.baseline = baseline
        if services:
            machine.baseline_status = BaselineStatus.CAPTURED.value
            self._upsert_baselines(session, machine, services)
            self._event(
                session,
                "baseline_captured_auto",
                payload={
                    "machine": machine.name,
                    "services": [s.get("container") for s in services],
                },
            )
            logger.info(
                "captured %d production service(s) on %s", len(services), machine.name
            )
        else:
            # Nothing is running, so there is nothing to put back and nothing
            # to clear — the machine is already ours. Note this is a MEASURED
            # emptiness: capture raises if it cannot reach the machine, so an
            # unreachable host never reads as a free one.
            machine.baseline_status = BaselineStatus.CLEARED.value
            self._event(
                session, "baseline_capture_found_nothing", payload={"machine": machine.name}
            )
            logger.info("no production services on %s; machine is free", machine.name)

    def _upsert_baselines(
        self, session: Session, machine: Machine, services: list[dict]
    ) -> None:
        """Promote each captured production service to a first-class baseline,
        keyed by (served_model_name, engine, card_type).

        The machine's capture stays the source of truth for RESTORE; this is the
        reusable reference a campaign compares against, no longer tied to the box
        it was captured on. A baseline a user set by hand is left alone — capture
        maintains only the ones it created, so an operator override is not
        silently overwritten the next night.
        """
        card_type = machine.gpu_type or ""
        for service in services:
            model = service.get("served_model_name")
            if not model:
                continue
            engine = engine_of_command(
                service.get("command") or service.get("docker_run")
            ) or "sglang"
            engine_args = service.get("engine_args")
            if engine_args is None:
                engine_args = baseline_engine_args(
                    service.get("command") or service.get("docker_run")
                )
            existing = session.scalars(
                select(Baseline).where(
                    Baseline.served_model_name == model,
                    Baseline.engine == engine,
                    Baseline.card_type == card_type,
                )
            ).first()
            if existing is None:
                session.add(
                    Baseline(
                        served_model_name=model, engine=engine, card_type=card_type,
                        engine_args=engine_args, source=f"capture:{machine.name}",
                    )
                )
            elif existing.source.startswith("capture"):
                existing.engine_args = engine_args
                existing.source = f"capture:{machine.name}"

    def _campaigns_waiting_on(self, session: Session, machine: Machine) -> list[Campaign]:
        """Campaigns that would start work on this machine right now."""
        return campaigns_waiting_on(session, machine, self.settings.default_max_run_minutes)

    def _maybe_clear(self, session: Session, machine: Machine) -> None:
        """Clear only once every campaign waiting on this machine has its OWN
        passing canary.

        A canary from an earlier campaign is stale evidence: it measured a
        different production state at a different time. Accepting it once let
        the lifecycle tear production down before this campaign's canary had
        even been scheduled — so the night ran with no baseline at all.
        """
        waiting = self._campaigns_waiting_on(session, machine)
        if not waiting:
            return

        canary_passed = None
        for campaign in waiting:
            if not self._in_place_canary_applies(session, campaign, machine):
                # No in-place canary is owed: either the campaign wants no
                # baseline, or production does not run the baseline config, so
                # the baseline is measured by relaunch instead. Clearing waits on
                # nothing here — the relaunch happens on the freed cards.
                continue
            canary = session.scalars(
                select(Run)
                .where(
                    Run.campaign_id == campaign.id,
                    Run.id.in_(run_ids_on_machine(machine.id)),
                    Run.kind == RunKind.BASELINE.value,
                )
                .order_by(Run.id.desc())
                .limit(1)
            ).first()
            if canary is None or canary.status != RunStatus.SUCCEEDED.value:
                # Not yet run, still running, or failed — either way production
                # stays up. A failed canary means the machine is suspect.
                return
            canary_passed = canary
        info = self._machine_info(machine)
        stopped = self._driver_for(info).clear_baseline(info, machine.baseline)
        machine.baseline_status = BaselineStatus.CLEARED.value
        self._event(
            session, "baseline_cleared_auto",
            payload={"machine": machine.name, "stopped": stopped,
                     "after_canary_run": canary_passed.id if canary_passed else None},
        )
        logger.info(
            "cleared production on %s (canary run %s)",
            machine.name, canary_passed.id if canary_passed else "not required",
        )

    def _maybe_restore(self, session: Session, machine: Machine) -> None:
        """Put production back once the night's window has closed.

        Whether it is owed is `restore_due` — the same function the Resources
        page reads to say "Restoring production". Asked twice, the two answers
        drifted: the page promised a restore this method was never going to do.
        """
        if not self.settings.auto_restore_production:
            return  # off by default: the admin owns the put-back, not us
        if not restore_due(session, machine):
            return
        live = session.scalars(
            select(Run)
            .where(
                Run.id.in_(run_ids_on_machine(machine.id)),
                Run.status.not_in([s.value for s in TERMINAL_RUN_STATES]),
            )
            .limit(1)
        ).first()
        if live is not None:
            return
        # A live policy session holds the machine the way runs do — its
        # self-served engines sit on the very ports a restore would reclaim.
        if machine_has_live_session(session, machine):
            return
        self._restore_production(session, machine, trigger="window_end")

    def _restore_production(self, session: Session, machine: Machine, trigger: str) -> None:
        """Put production back and check that what came back is what we took.

        Shared by the two things that can owe a machine its service: a
        campaign window closing, and a lease ending. They are different
        decisions with the same consequence, and having written the
        consequence twice is how one of them would eventually skip
        verification.
        """
        info = self._machine_info(machine)
        driver = self._driver_for(info)
        restored = driver.restore_baseline(info, machine.baseline)
        findings = driver.verify_baseline(info, machine.baseline)
        drift = [f for f in findings if not f.get("ok")]
        machine.baseline_status = BaselineStatus.RESTORED.value
        machine.baseline = {**machine.baseline, "restore_verification": findings}
        self._event(
            session,
            "baseline_restored_auto" if not drift else "baseline_restored_auto_with_drift",
            payload={"machine": machine.name, "restored": restored,
                     "trigger": trigger, "verification": findings},
        )
        level = logger.warning if drift else logger.info
        level("restored production on %s (%s; drift: %s)", machine.name, trigger, drift or "none")

    def _last_cleared_at(self, session: Session, machine: Machine) -> datetime | None:
        return last_cleared_at(session, machine)

    def _has_valid_candidates(self, session: Session, campaign: Campaign) -> bool:
        return has_valid_candidates(session, campaign)

    def _campaigns_using(self, session: Session, machine: Machine) -> list[Campaign]:
        return campaigns_using(session, machine)

    # ------------------------------------------------------- policy sessions

    def _advance_policy_sessions(self, session: Session) -> None:
        """One step of every live policy session (see orchestrator/policy_session.py).

        Imported lazily and held on the instance: the engine shares this
        supervisor's drivers and evaluators, and constructing it per tick
        would re-resolve both every 10 seconds.
        """
        if self._policy_engine is None:
            from app.control.orchestrator.policy_session import PolicySessionEngine

            self._policy_engine = PolicySessionEngine(self)
        self._policy_engine.advance_all(session)

    def _release_machine_if_idle(self, session: Session, machine: Machine) -> None:
        """RESERVED → AVAILABLE, but only when nothing is left holding the box:
        no live run, no container still tearing down, and no live policy
        session. Runs, the teardown janitor and sessions each call this on their
        own terminal path; whichever ends last actually frees it."""
        if machine.state != MachineState.RESERVED.value:
            return
        if self._live_runs_on(session, machine):
            return
        if self._teardown_pending_on(session, machine):
            return
        if machine_has_live_session(session, machine):
            return
        machine.state = MachineState.AVAILABLE.value

    def _reap_teardowns(self, session: Session) -> None:
        """Finish tearing down containers that did not go quietly.

        A run marked teardown_pending is terminal for every other purpose, but
        still holds its cards until its container is confirmed gone. Each tick,
        per such run: confirm it vanished (and free the machine), or — once the
        graceful grace has elapsed — force it, or — once even that has run out
        of patience — quarantine the machine so the next run is never placed on
        top of a container that would not leave."""
        pending = session.scalars(
            select(Run).where(Run.teardown_pending.is_(True))
        ).all()
        if not pending:
            return
        grace = self.settings.teardown_grace_seconds
        giveup = self.settings.teardown_giveup_seconds
        for run in pending:
            try:
                spec = self._spec_for(session, run)
                driver = self._driver_for(spec.machine)
                handle = driver.attach(spec)
            except Exception:
                logger.exception("teardown reap for run %d failed to attach", run.id)
                continue
            if handle is None:
                self._confirm_teardown(session, run)
                continue
            elapsed = (_now() - (_as_utc(run.finished_at) or _now())).total_seconds()
            if elapsed >= giveup:
                # It has ignored SIGTERM and repeated forced removals for the
                # whole budget. Stop hammering and hold the box out of rotation
                # for a human; its cards are still reserved by this run's row.
                # Quarantine the machine ACTUALLY stuck: for a gang that is not
                # necessarily the master, and taking an innocent sibling out of
                # rotation would punish the wrong box.
                try:
                    stuck = driver.unfinished(handle) or [handle]
                    names = [node.machine.name for node in stuck]
                except Exception:
                    logger.exception("could not tell which node of run %d is stuck", run.id)
                    names = [run.machine.name] if run.machine is not None else []
                machines = (
                    session.scalars(select(Machine).where(Machine.name.in_(names))).all()
                    if names
                    else []
                )
                for machine in machines or ([run.machine] if run.machine else []):
                    self._quarantine_machine(
                        session, machine,
                        f"run {run.id} on {machine.name} would not tear down after "
                        f"{int(elapsed)}s",
                    )
                run.teardown_pending = False
                continue
            if elapsed >= grace:
                try:
                    driver.teardown(handle)  # SIGTERM ignored; force the removal
                    run.teardown_attempts += 1
                except Exception:
                    logger.exception("forced teardown for run %d failed", run.id)
            # else: still inside the graceful window — let the SIGTERM land.

    def _release_run_machines(self, session: Session, run: Run) -> None:
        """Offer every machine this run occupied back to the fleet.

        For a single-node run that is the one machine `runs.machine_id` names.
        For a gang it is EVERY member: releasing only the master leaves the
        workers reserved for the rest of the deployment's life, which shows up
        as a group that can never be placed again and nothing to explain it.
        """
        released = False
        for node in nodes_of(session, run.id):
            if node.machine_id is None:
                continue
            machine = session.get(Machine, node.machine_id)
            if machine is not None:
                self._release_machine_if_idle(session, machine)
                released = True
        if not released and run.machine is not None:
            # No node rows to read (an old run, or a test double): the master's
            # own column is still the truth for it.
            self._release_machine_if_idle(session, run.machine)

    def _confirm_teardown(self, session: Session, run: Run) -> None:
        run.teardown_pending = False
        self._event(
            session, "run_teardown_confirmed",
            campaign_id=run.campaign_id, run_id=run.id,
            payload={"attempts": run.teardown_attempts},
        )
        self._release_run_machines(session, run)

    def _quarantine_machine(
        self, session: Session, machine: Machine | None, reason: str
    ) -> None:
        if machine is None or machine.needs_attention:
            return
        machine.needs_attention = True
        machine.attention_reason = reason
        machine.attention_at = _now()
        self._event(
            session, "machine_quarantined",
            payload={"machine": machine.name, "reason": reason},
        )
        logger.warning("machine %s quarantined: %s", machine.name, reason)

    @staticmethod
    def _machine_info(machine: Machine) -> MachineInfo:
        return MachineInfo.of(machine)

    def _driver_for(self, machine: MachineInfo) -> DeploymentDriver:
        """The driver that reaches this machine.

        A machine that names no substrate — and every test that injects a fake
        by assigning `self.driver` — gets the platform default. One that names a
        different one (k8s during the migration) gets that, built once and
        cached. Resolving off the MachineInfo means the same spec/handle that
        crosses a driver boundary carries the answer with it, so launch, poll
        and teardown of one run always reach the same substrate.
        """
        wanted = machine.driver or self.driver.name
        cluster_id = getattr(machine, "cluster_id", None)
        # The injected/default driver is only the answer when the machine names
        # no cluster: two k8s clusters are two different drivers even though
        # both are substrate "k8s".
        if wanted == self.driver.name and not cluster_id:
            return self.driver
        key = (wanted, cluster_id)
        cached = self._drivers.get(key)
        if cached is None:
            cached = get_driver(wanted, cluster_id=cluster_id)
            self._drivers[key] = cached
        return cached

    def _process_stop_requests(self, session: Session) -> None:
        """Users request stops via the API (a stop_requested event); only the
        supervisor actually touches resources."""
        live_runs = {
            run.id: run
            for run in session.scalars(
                select(Run).where(Run.status.not_in([s.value for s in TERMINAL_RUN_STATES]))
            ).all()
        }
        if not live_runs:
            return
        stop_events = session.scalars(
            select(Event).where(
                Event.kind == "stop_requested", Event.run_id.in_(live_runs.keys())
            )
        ).all()
        for event in stop_events:
            run = live_runs.pop(event.run_id, None)
            if run is None:
                continue
            if run.llmbench_submission_id:
                self.bench.cancel(run.llmbench_submission_id)
            self._finish(session, run, RunStatus.KILLED, error=USER_STOP_ERROR)

    def reconcile(self) -> None:
        """On worker startup: re-attach to whatever was in flight."""
        with self.session_factory() as session:
            live_runs = session.scalars(
                select(Run).where(
                    Run.status.not_in([s.value for s in TERMINAL_RUN_STATES])
                )
            ).all()
            for run in live_runs:
                if run.status == RunStatus.PENDING.value:
                    continue  # nothing external yet
                if run.kind in (RunKind.BASELINE.value, RunKind.EXTERNAL.value):
                    # These runs never owned a container named after them —
                    # attach() by autotune-run-<id> would always come back None
                    # and "orphan" a perfectly healthy measurement on every
                    # worker restart. Their endpoint is probed on the next
                    # tick's advance, which is the real liveness check.
                    logger.info("re-attached run %d in state %s (no container)", run.id, run.status)
                    continue
                spec = self._spec_for(session, run)
                handle = self._driver_for(spec.machine).attach(spec)
                if handle is None and run.status != RunStatus.BENCHING.value:
                    # container vanished while we were away and no external
                    # benchmark to resume → fail it; benching runs can still be
                    # polled via the stored submission id even if the container
                    # died (poll will surface that).
                    self._fail(session, run, "orphaned", "container gone after worker restart")
                else:
                    logger.info("re-attached run %d in state %s", run.id, run.status)
            session.commit()

    # ------------------------------------------------------------------ plan

    def _plan(self, session: Session) -> None:
        """Turn a campaign's declared search space into candidate rows.

        This is enumeration, not search: every point the space declares, once,
        in the order the space declares it. There is no strategy here and no
        pluggable one — a campaign that wants to be clever about WHICH points
        to spend the night on names a policy container instead, and then its
        candidates arrive over the policy session API and this step does
        nothing (`api/policy_sessions.py`).

        Enumerating is still worth doing in-process: it is the same expansion
        `coverage` and the UI's candidate count already use, so what the
        platform runs and what it told you it would run cannot drift.
        """
        campaigns = session.scalars(
            select(Campaign).where(
                Campaign.status == CampaignStatus.ACTIVE.value,
                Campaign.policy_id.is_(None),
            )
        ).all()
        for campaign in campaigns:
            if not (campaign.search_space or {}):
                # Nothing declared at all. Not the same as a space whose only
                # content is a `base`, which is one deliberate configuration —
                # this is a campaign whose configs arrive some other way.
                continue
            existing_hashes = set(
                session.scalars(
                    select(Candidate.config_hash).where(Candidate.campaign_id == campaign.id)
                ).all()
            )
            gpu_count = self._validation_gpu_count(session, campaign)
            for config in expand(campaign.search_space or {}):
                point = CandidateConfig(engine_args=config)
                if point.hash in existing_hashes:
                    continue
                existing_hashes.add(point.hash)
                error = validate_config(
                    config,
                    ValidationContext(
                        gpu_count=gpu_count,
                        engine=campaign.engine,
                        nodes=self._campaign_nodes(session, campaign),
                    ),
                )
                session.add(
                    Candidate(
                        campaign_id=campaign.id,
                        config=config,
                        config_hash=point.hash,
                        status=(
                            CandidateStatus.INVALID.value if error else CandidateStatus.VALID.value
                        ),
                        validation_error=error or "",
                    )
                )
                self._event(
                    session,
                    "candidate_created" if not error else "candidate_rejected_static",
                    campaign_id=campaign.id,
                    payload={"config": config, "error": error or ""},
                )

    # -------------------------------------------------------------- schedule

    def _schedule(self, session: Session) -> None:
        campaigns = session.scalars(
            select(Campaign).where(Campaign.status == CampaignStatus.ACTIVE.value)
        ).all()
        for campaign in campaigns:
            if not self._window_allows_new_run(campaign):
                continue

            # A single-benchmark campaign whose one benchmark is a replay must
            # not start ANY run — canary, baseline or candidate — until its
            # dataset build settles, or the first run measures a different slice
            # of traffic than the last. A staged campaign screens on the cheap
            # benchmark while the replay dataset builds, so it is NOT held here;
            # its verify stage waits in _maybe_finish_campaign instead.
            if not is_staged(campaign) and self._waiting_on_dataset(campaign):
                continue

            # The night's control run, the shortcut path: benchmark the handed-
            # over production service in place, BEFORE it is torn down, when it
            # already runs the baseline config. Anchors cross-night comparison
            # and catches an unhealthy machine before any experiment burns time.
            canary_on = next(
                (
                    machine
                    for machine in self._usable_machines(session, campaign)
                    if self._baseline_canary_due(session, campaign, machine)
                ),
                None,
            )
            if canary_on is not None:
                self._schedule_baseline_canary(session, campaign, canary_on)
                continue

            # A policy campaign keeps ONLY the canary above: its machine gate
            # (capture → canary → clear) is shared, but its runs come from the
            # session, and a relaunch baseline here would eat the session's
            # cards for the whole screening benchmark.
            if campaign.policy_id:
                continue

            # No shortcut applied — measure the baseline by relaunching it, as
            # one more config the pipeline screens (and later verifies).
            self._ensure_relaunch_baseline(session, campaign)

            # Keep placing until the machines are full: an 8-card node running a
            # single tp=2 config leaves six cards idle all night.
            while self._start_one(session, campaign):
                pass

    def _start_one(self, session: Session, campaign: Campaign) -> bool:
        """Place one candidate on one machine, if anything fits. True if it did."""
        pending = session.scalars(
            select(Candidate)
            .where(
                Candidate.campaign_id == campaign.id,
                Candidate.status == CandidateStatus.VALID.value,
            )
            .order_by(Candidate.id)
        ).all()
        # An IN-PLACE baseline rides a Candidate row to reuse the run machinery,
        # but its run measures the deployed service and never launches. If one
        # reaches this queue — "Retry failed" re-validating a failed canary, or a
        # legacy `--__baseline__ <container>` sentinel config — launching it would
        # render an un-launchable config after a full model load. Retired, not
        # skipped, so it also cannot keep the campaign out of DONE.
        #
        # A RELAUNCH baseline is the deliberate inverse: it carries production's
        # real engine args and is MEANT to launch — that is how the reference is
        # measured when production does not already serve it — so it passes
        # through untouched.
        for candidate in [c for c in pending if is_in_place_baseline(c)]:
            candidate.status = CandidateStatus.EXHAUSTED.value
            pending.remove(candidate)
            self._event(
                session, "baseline_candidate_retired", campaign_id=campaign.id,
                payload={"candidate": candidate.id, "config": candidate.config},
            )
            logger.info(
                "retired in-place baseline candidate %d; it is measured in place, "
                "not launched", candidate.id,
            )

        # A verification run takes several times as long as a screening one.
        # The window check at the top of `_schedule` reserved the screening
        # figure, which is why this is asked again per candidate: starting an
        # hour-long replay ninety minutes before the hand-back means killing it
        # at the cutoff with nothing to show and the slot spent.
        pending = [c for c in pending if self._window_allows_stage(campaign, stage_of(c))]

        if not pending:
            self._maybe_finish_campaign(session, campaign)
            return False
        by_id = {c.id: c for c in pending}
        placements = [Placement(c.id, _cards_used(c.config)) for c in pending]

        # A group campaign places a GANG, not a run on one machine: the group is
        # the unit, and the single-machine packer below would put a 2-node config
        # on one box (or refuse it) — the exact deployment the operator did not
        # ask for. Handled entirely separately so the two paths cannot half-apply.
        if campaign.node_group:
            return self._start_gang(session, campaign, pending, by_id)

        for machine in self._usable_machines(session, campaign):
            # Experiments need production off the machine: captured (so it can
            # be restored) and cleared (so it is not competing for GPUs and
            # skewing every measurement).
            #
            # CLEARED is the only state that says so. NONE used to be accepted
            # too, on the assumption that a machine we had never inspected was
            # empty — but that is precisely what we have not checked. A machine
            # capture could not reach stays NONE, and launching there would put
            # experiments next to whatever is already serving.
            if machine.baseline_status != BaselineStatus.CLEARED.value:
                continue

            # A machine deployed as part of a live multi-node run is closed to
            # single-node work even on cards the gang is not using: its ranks
            # share the host and the interconnect, and a co-tenant engine would
            # contend for both, which is the skew the gang's measurement cannot
            # afford. The other direction — not placing a gang on a member that
            # is busy — is the group gate. Both are time-scoped: once
            # the gang finishes, the member is an ordinary machine again, so
            # grouping a pair of boxes does not reserve them against the
            # single-node work they do the rest of the night.
            if machine_hosts_live_gang(session, machine.id):
                continue

            live = self._live_runs_on(session, machine)
            if live and not self._may_share(session, campaign, live):
                continue
            # A container still tearing down holds its cards and port until the
            # janitor confirms it gone — count it alongside live runs so the
            # next placement never lands on hardware that is not yet free.
            blockers = live + self._teardown_pending_on(session, machine)
            # Card occupancy is read per NODE: on a gang's worker the run row
            # carries rank 0's cards, and the worker's own node carries its own.
            # A single-node node mirrors its run, so this is exactly today's
            # answer for every run that exists.
            nodes = self._live_nodes_on(session, machine) + self._teardown_pending_nodes_on(
                session, machine
            )
            # A node that recorded no cards holds an unknown set of them — runs
            # from before per-GPU pinning, and the baseline canary, which
            # measures production while production owns every card. Unknown
            # must read as "all", not "none": read as none, the scheduler
            # placed two experiments on top of a run using the whole machine.
            if machine.gpu_count > 0 and any(not (n.gpu_indices or []) for n in nodes):
                continue

            if machine.gpu_count <= 0:
                # A CPU-only box (the mock/test host) has no cards to divide, so
                # it hosts exactly one run and pins nothing.
                if blockers:
                    continue
                chosen, cards = by_id[placements[0].candidate_id], []
            else:
                taken = {index for node in nodes for index in (node.gpu_indices or [])}
                free = free_indices(machine.gpu_count, taken)
                placement = choose_next(len(free), placements)
                if placement is None:
                    continue  # nothing pending fits in what is left
                chosen = by_id[placement.candidate_id]
                cards = free[: placement.cards]

            self._create_run(session, campaign, machine, chosen, cards, blockers)
            return True
        return False

    def _start_gang(
        self,
        session: Session,
        campaign: Campaign,
        pending: list[Candidate],
        by_id: dict[int, Candidate],
    ) -> bool:
        """Place one candidate across the WHOLE node group, or nothing at all.

        A gang is not packed into whatever is free: it takes every member at once
        and exclusively. Its ranks share an interconnect and a host, so a
        co-tenant engine would contend for both — which is exactly the skew the
        measurement cannot afford — and a gang on half its members is not a
        smaller version of the deployment, it is a broken one.

        Waits, rather than falls back: if any member is busy, unleased or not
        handed over, the campaign simply does not start this tick. The members
        stay in the single-node fleet the rest of the time, so waiting for them
        to be free costs the night nothing.
        """
        group = self._group_for_campaign(session, campaign)
        if group is None:
            return False
        ranked = members_by_rank(session, group)
        nodes = len(ranked)
        if nodes < 2:
            # A one-member group is one machine; there is no gang to place.
            # Leaving it to the single-node packer would be right, but the
            # campaign pinned a group, so say so instead of silently ignoring it.
            return False
        if not self._group_is_idle(session, group):
            return False
        members = [machine for _, machine in ranked]
        # Defensive: campaign save refuses a non-ssh group, but a machine's
        # substrate can be edited afterwards. Launching N separate k8s
        # Deployments would be N single-node engines, not a gang, so wait rather
        # than mislaunch.
        if any((machine.driver or "ssh_docker") != "ssh_docker" for machine in members):
            logger.info(
                "node group %s spans a non-ssh substrate; multi-node launch is not "
                "implemented for it yet", group.name,
            )
            return False
        per_node_capacity = min(machine.gpu_count for machine in members)

        fitting: list[tuple[Candidate, int]] = []
        for candidate in pending:
            world = _cards_used(candidate.config)
            # The world must split evenly and fit on every member; a candidate
            # that cannot is invalid, not merely unplaceable, and the validator
            # has already rejected it. Skipping here is the belt to that braces.
            if world < nodes or world % nodes:
                continue
            if world // nodes > per_node_capacity:
                continue
            fitting.append((candidate, world // nodes))
        if not fitting:
            return False
        # Widest per-node first, ties by id: the same rule the single-node packer
        # uses, so a campaign's order is reproducible from the database.
        candidate, per_node = min(fitting, key=lambda pair: (-pair[1], pair[0].id))

        plan = [(machine, list(range(per_node))) for machine in members]
        port = self._free_port_across(session, campaign, members)
        master = members[0]
        dist_port = self._free_dist_port(session, master, exclude={port})
        self._create_run(
            session, campaign, master, candidate, plan[0][1], [],
            node_plan=plan, service_port=port, dist_port=dist_port,
            node_group=group.name,
        )
        return True

    def _free_port_across(
        self, session: Session, campaign: Campaign, machines: list[Machine]
    ) -> int:
        """A service port free on EVERY member.

        sglang binds the same `--port` on each rank, so a port taken on any one
        machine is taken for the whole gang. The single-node allocator cannot see
        that — it knows one host — which is why this exists rather than being a
        parameter of `_free_port`.
        """
        used: set[int] = set()
        for machine in machines:
            for run in self._live_runs_on(session, machine) + self._teardown_pending_on(
                session, machine
            ):
                used.add(run.service_port or (run.campaign.service_port if run.campaign else 0))
        used.discard(0)
        port = campaign.service_port
        for _ in range(256):
            if port not in used and not 30000 <= port <= 32767:
                return port
            port += 1
        return campaign.service_port  # give up; the driver's pre-check will refuse

    def _free_dist_port(
        self, session: Session, master: Machine, exclude: set[int] | None = None
    ) -> int:
        """A rendezvous port free on the master.

        Only the master needs it — that is where every rank dials — but it must
        not collide with the gang's own service port or with anything a live
        co-tenant holds there. 0 is not a valid answer: an absent rendezvous
        would launch the ranks into a rendezvous that never happens.
        """
        used = set(exclude or ())
        for run in self._live_runs_on(session, master) + self._teardown_pending_on(
            session, master
        ):
            if run.dist_port:
                used.add(run.dist_port)
            if run.service_port:
                used.add(run.service_port)
        port = DIST_PORT_BASE
        for _ in range(256):
            if port not in used and not 30000 <= port <= 32767:
                return port
            port += 1
        return DIST_PORT_BASE

    def _create_run(
        self,
        session: Session,
        campaign: Campaign,
        machine: Machine,
        candidate: Candidate,
        cards: list[int],
        blockers: list[Run],
        *,
        node_plan: list[tuple[Machine, list[int]]] | None = None,
        service_port: int | None = None,
        dist_port: int = 0,
        node_group: str = "",
    ) -> None:
        for member, _ in (node_plan or [(machine, cards)]):
            member.state = MachineState.RESERVED.value
        run = Run(
            campaign_id=campaign.id,
            candidate_id=candidate.id,
            machine_id=machine.id,
            status=RunStatus.PENDING.value,
            gpu_indices=cards,
            service_port=(
                service_port if service_port is not None
                else self._free_port(campaign, blockers)
            ),
            dist_port=dist_port,
            node_group=node_group,
            started_at=_now(),
        )
        candidate.status = CandidateStatus.EXHAUSTED.value
        session.add(run)
        session.flush()
        run.container_name = f"autotune-run-{run.id}"
        # The mapper event wrote rank 0 from the run's master columns above. A
        # gang's WORKERS are the machines `runs.machine_id` does not name, so they
        # are explicit rows — and they are what every "which machines does this
        # run occupy" query reads, including the exclusivity rule.
        for rank, (member, member_cards) in enumerate(node_plan or []):
            if rank == 0:
                continue
            session.add(
                RunNode(
                    run_id=run.id,
                    machine_id=member.id,
                    rank=rank,
                    is_master=False,
                    gpu_indices=list(member_cards),
                    service_port=run.service_port,
                    container_name=f"autotune-run-{run.id}-r{rank}",
                    status=RunStatus.PENDING.value,
                )
            )
        self._event(
            session,
            "run_scheduled",
            campaign_id=campaign.id,
            run_id=run.id,
            payload={
                "machine": machine.name,
                "config": candidate.config,
                "gpus": cards,
                "port": run.service_port,
                "sharing_with": [r.id for r in blockers],
                # A gang's plan, so the audit trail says which boxes were taken
                # together rather than only which one is the master.
                "nodes": [member.name for member, _ in (node_plan or [])],
                "dist_port": dist_port,
                "node_group": node_group,
            },
        )

    def _release_queued_while_paused(self, session: Session) -> None:
        """A paused campaign gives back its place in the cluster's queue.

        On a shared GPU cluster an unplaced run is pure demand: a pod sitting in
        the scheduler's queue, competing with production for the next card that
        frees. Pausing is how a human says "prod needs the cluster back", so
        holding that queue slot is the one thing a paused campaign should not do.
        The run is withdrawn, its candidate goes back to the pool, and resuming
        re-submits it — nothing is lost, because an unplaced pod has done no
        work and holds no hardware.

        Two deliberate limits:

        - Only the UNPLACED. A pod the scheduler has already bound to a node is
          not competing for cards any more, it won them — killing it would throw
          away a model load that may be minutes from ready. `placement()`
          answers this per substrate, and answers "placed" for anything it
          cannot tell, so an unreadable cluster costs us nothing.
        - Only what a scheduler can queue. ssh_docker has no queue to leave, and
          "pause means frozen" is a hard-earned rule there (node-85, 2026-08-19):
          a pause must leave the box exactly as it is. The default `placement()`
          says "placed", so those machines are untouched without naming them.
        """
        paused = session.scalars(
            select(Campaign).where(Campaign.status == CampaignStatus.PAUSED.value)
        ).all()
        for campaign in paused:
            runs = session.scalars(
                select(Run).where(
                    Run.campaign_id == campaign.id,
                    Run.status.not_in([s.value for s in TERMINAL_RUN_STATES]),
                )
            ).all()
            for run in runs:
                if run.kind in (RunKind.BASELINE.value, RunKind.EXTERNAL.value):
                    continue  # not ours to withdraw
                try:
                    spec = self._spec_for(session, run)
                    driver = self._driver_for(spec.machine)
                    # Ask whether this substrate HAS a queue before touching it:
                    # attach() is an ssh round-trip on a bare-metal box, and a
                    # paused campaign would otherwise pay one per run per tick
                    # to be told the same thing forever. A driver that never
                    # overrode placement() cannot queue, by construction.
                    if type(driver).placement is DeploymentDriver.placement:
                        continue
                    handle = driver.attach(spec)
                    if handle is None or driver.placement(handle) != "queued":
                        continue
                except Exception:
                    logger.exception("placement check for run %d failed", run.id)
                    continue
                self._release_to_queue(session, run)

    def _release_to_queue(self, session: Session, run: Run) -> None:
        """Withdraw a run and put its candidate back, charging it nothing.

        Distinct from a failure in the one way that matters: `_requeue_if_
        infrastructure` counts a candidate's past runs to bound its retries, and
        a run WE took back says nothing about whether the config can work. Left
        uncounted, pausing three times would otherwise exhaust a candidate that
        has never once been tried.
        """
        run.failure_class = RELEASED
        self._finish(
            session, run, RunStatus.KILLED,
            error="withdrawn while the campaign was paused; the cluster queue slot "
                  "was given back and this candidate will be submitted again on resume",
        )
        run.candidate.status = CandidateStatus.VALID.value
        self._event(
            session, "run_released_to_queue",
            campaign_id=run.campaign_id, run_id=run.id,
            payload={"candidate_id": run.candidate_id, "reason": "campaign_paused"},
        )
        logger.info(
            "withdrew queued run %d (candidate %d) — campaign paused",
            run.id, run.candidate_id,
        )

    def _live_runs_on(self, session: Session, machine: Machine) -> list[Run]:
        """Runs occupying this machine, whether as master or as a worker.

        Membership now comes from `run_nodes`, not `runs.machine_id`: a gang's
        worker machines are occupied by a run whose `machine_id` points at the
        master, and a query on the run row alone would see those machines as
        free. For a single-node run — every run until multi-node launches — the
        rank-0 node aliases `machine_id`, so the answer is identical.
        """
        return live_runs_on(session, machine)

    def _teardown_pending_on(self, session: Session, machine: Machine) -> list[Run]:
        """Terminal runs on this machine whose container is not yet confirmed
        gone. Finished for every other purpose, but still holding their cards
        and port until the janitor sees them vanish — so a placement never
        lands on hardware a dying container has not released."""
        return teardown_pending_on(session, machine)

    def _live_nodes_on(self, session: Session, machine: Machine) -> list[RunNode]:
        """The live NODES on this machine — what its cards and port are held by.

        The card math needs the node's own `gpu_indices`, not the run's: on a
        gang's worker those are different (rank 0's cards live in the run row),
        and reading the run's would under-count the worker's occupancy.
        """
        return list(
            session.scalars(
                select(RunNode)
                .join(Run, Run.id == RunNode.run_id)
                .where(
                    RunNode.machine_id == machine.id,
                    Run.status.not_in([s.value for s in TERMINAL_RUN_STATES]),
                )
            ).all()
        )

    def _teardown_pending_nodes_on(self, session: Session, machine: Machine) -> list[RunNode]:
        """`_teardown_pending_on` at node granularity, for the same card math."""
        return list(
            session.scalars(
                select(RunNode)
                .join(Run, Run.id == RunNode.run_id)
                .where(
                    RunNode.machine_id == machine.id,
                    Run.teardown_pending.is_(True),
                )
            ).all()
        )

    def _may_share(self, session: Session, campaign: Campaign, live: list[Run]) -> bool:
        """Both sides must opt in: a campaign that wants the machine to itself
        gets it, and never has someone else's run land on top of it."""
        if not campaign.share_machine:
            return False
        campaign_ids = {run.campaign_id for run in live}
        others = session.scalars(
            select(Campaign).where(Campaign.id.in_(campaign_ids or {0}))
        ).all()
        return all(other.share_machine for other in others)

    def _free_port(
        self, campaign: Campaign, live: list[Run], reserved: set[int] | None = None
    ) -> int:
        """A port no live run on this machine already holds.

        Co-tenants share the host network namespace, so the campaign's single
        configured port cannot serve two engines at once. A run that predates
        per-run ports recorded 0 but is listening on its campaign's configured
        port — take that as its port rather than as "no port". `reserved` adds
        ports promised to someone who has not launched on them yet (a policy
        session's block on a shared box).
        """
        used = {
            run.service_port or (run.campaign.service_port if run.campaign else 0)
            for run in live
        }
        used |= set(reserved or ())
        used.discard(0)
        port = campaign.service_port
        for _ in range(256):
            # 30000-32767 is the k8s NodePort range: kube-proxy hijacks it on
            # the node IP, leaving the engine reachable only from localhost.
            if port not in used and not 30000 <= port <= 32767:
                return port
            port += 1
        return campaign.service_port  # give up; the driver's pre-check will refuse

    def _baseline_canary_due(self, session: Session, campaign: Campaign, machine: Machine) -> bool:
        """Is an in-place baseline canary owed right now?

        The canary is a speedup, not a duty: measuring the deployed service in
        place only stands in for the baseline when production already runs the
        baseline config. When it does not, there is nothing to shortcut — the
        baseline is measured by relaunching it — so the canary is not due and
        clearing production is not gated on one."""
        return (
            canary_state(session, campaign, machine) == CANARY_DUE
            and self._in_place_canary_applies(session, campaign, machine)
        )

    def _campaign_baseline(
        self, session: Session, campaign: Campaign, machine: Machine | None = None
    ) -> Baseline | None:
        """The production reference for what this campaign tunes, resolved by
        card type. A machine pins the card type; without one, the first usable
        machine's does — a campaign's machines are one card type in practice."""
        card_type = machine.gpu_type if machine is not None else ""
        if not card_type:
            card_type = next(
                (m.gpu_type for m in self._usable_machines(session, campaign) if m.gpu_type),
                "",
            )
        return resolve_baseline(
            session, campaign.served_model_name, campaign.engine, card_type or ""
        )

    def _in_place_canary_applies(
        self, session: Session, campaign: Campaign, machine: Machine
    ) -> bool:
        """Does production on this machine already serve the baseline config?

        The one condition under which measuring the deployed service in place is
        a valid stand-in for relaunching the baseline — and the thing `_maybe_
        clear` reads to know whether clearing waits on a canary at all."""
        if not campaign.run_baseline_canary:
            return False
        baseline = self._campaign_baseline(session, campaign, machine)
        if baseline is None:
            # No reference was defined, so there is no "other" config to prefer:
            # production IS the baseline, and measuring it in place is the only
            # sensible thing. (Capture upserts a reference on hand-over, so this
            # is the pre-capture / no-baseline case, not the common one.)
            return True
        for service in (machine.baseline or {}).get("services", []):
            config = service.get("engine_args") or baseline_engine_args(
                service.get("command") or service.get("docker_run")
            )
            if same_config(config, baseline.engine_args):
                return True
        return False

    def _canaries_since_activation(
        self, session: Session, campaign: Campaign, machine: Machine
    ) -> list[Run]:
        return canaries_since_activation(session, campaign, machine)

    def _last_activation(self, session: Session, campaign: Campaign) -> datetime:
        return last_activation(session, campaign)

    def _schedule_baseline_canary(
        self, session: Session, campaign: Campaign, machine: Machine
    ) -> None:
        """A baseline run measures what is already deployed: nothing to launch,
        nothing to tear down. It starts at the health gate."""
        services = (machine.baseline or {}).get("services", [])
        service = next((s for s in services if s.get("endpoint_url")), None)
        if service is None:
            return

        # Production's real engine config, unified with search configs: the
        # canary reads, renders and card-normalizes exactly like a candidate.
        # Fall back to parsing the command for services captured before the
        # config was stored. The run is never launched (it starts at the health
        # gate against the deployed service), so carrying real args is safe.
        config = dict(
            service.get("engine_args")
            or baseline_engine_args(service.get("command") or service.get("docker_run"))
        )
        candidate = Candidate(
            campaign_id=campaign.id,
            config=config,
            config_hash=f"baseline-{machine.id}",
            kind=CandidateKind.BASELINE.value,
            is_baseline=True,
            status=CandidateStatus.EXHAUSTED.value,
        )
        session.add(candidate)
        session.flush()

        machine.state = MachineState.RESERVED.value
        run = Run(
            campaign_id=campaign.id,
            candidate_id=candidate.id,
            machine_id=machine.id,
            kind=RunKind.BASELINE.value,
            status=RunStatus.HEALTH_CHECK.value,  # already deployed
            endpoint_url=service["endpoint_url"],
            container_name=service.get("container", ""),
            started_at=_now(),
        )
        session.add(run)
        session.flush()
        self._event(
            session, "baseline_canary_scheduled", campaign_id=campaign.id, run_id=run.id,
            payload={"machine": machine.name, "endpoint": service["endpoint_url"]},
        )

    def _has_baseline_candidate(
        self, session: Session, campaign: Campaign, stage: str
    ) -> bool:
        """Has this campaign already got a baseline for `stage` — measured in
        place or relaunched, run or still pending? Created once per stage."""
        candidates = session.scalars(
            select(Candidate).where(Candidate.campaign_id == campaign.id)
        ).all()
        return any(
            is_baseline_candidate(c) and stage_of(c) == stage for c in candidates
        )

    def _add_baseline_candidate(
        self, session: Session, campaign: Campaign, baseline: Baseline, kind: str
    ) -> Candidate:
        """A baseline the pipeline launches and measures itself, at one stage.

        `is_baseline` is what keeps it out of the search's own ranking (it is the
        reference, not a competitor) while the leaderboard reads it as that
        stage's anchor. `kind` places the stage: SEARCH screens, VERIFICATION
        verifies."""
        candidate = Candidate(
            campaign_id=campaign.id,
            config=dict(baseline.engine_args or {}),
            config_hash=f"baseline-{kind}-{baseline.id}",
            kind=kind,
            is_baseline=True,
            status=CandidateStatus.VALID.value,
        )
        session.add(candidate)
        session.flush()
        self._event(
            session, "baseline_relaunch_scheduled", campaign_id=campaign.id,
            payload={"baseline_id": baseline.id, "stage": stage_of(candidate),
                     "config": candidate.config},
        )
        return candidate

    def _ensure_relaunch_baseline(self, session: Session, campaign: Campaign) -> None:
        """Measure the baseline by relaunching it, when the in-place shortcut did
        not apply. One screen baseline now; the verify baseline is added with the
        shortlist in `_schedule_verification`, once there is a dataset to pin."""
        if not campaign.run_baseline_canary:
            return
        if self._has_baseline_candidate(session, campaign, SCREEN):
            return
        baseline = self._campaign_baseline(session, campaign)
        if baseline is None:
            return
        self._add_baseline_candidate(session, campaign, baseline, CandidateKind.SEARCH.value)

    # --------------------------------------------------------------- advance

    def _advance_runs(self, session: Session) -> None:
        live_runs = session.scalars(
            select(Run).where(Run.status.not_in([s.value for s in TERMINAL_RUN_STATES]))
        ).all()
        for run in live_runs:
            try:
                self._advance_one(session, run)
            except Exception as exc:  # one bad run must not stall the fleet
                logger.exception("advancing run %d failed", run.id)
                # Put the traceback ON THE RUN, not only in the worker log:
                # otherwise the UI shows a dead run with no explanation and
                # diagnosing it needs shell access to the platform host.
                self._fail(session, run, "supervisor_error", _crash_detail(exc))

    def _advance_one(self, session: Session, run: Run) -> None:
        # EXTERNAL rides the baseline path on purpose: both measure an endpoint
        # the platform did not launch — health-check, bench, never tear down.
        if run.kind in (RunKind.BASELINE.value, RunKind.EXTERNAL.value):
            self._advance_baseline(session, run)
            return

        spec = self._spec_for(session, run)
        driver = self._driver_for(spec.machine)

        if run.status == RunStatus.PENDING.value:
            try:
                handle, launch_commands = driver.launch_nodes(spec)
            except RuntimeError as exc:
                # e.g. port already in use, docker run rejected — classify from
                # the error text so the search sees a meaningful failure class.
                self._fail(session, run, classify_failure(str(exc)), str(exc)[:2000])
                return
            # The run keeps the MASTER's command; every rank's own command is
            # recorded on its node row, which is what makes a gang reproducible
            # node by node (the run row's single text column cannot hold them).
            run.launch_command = launch_commands[0] if launch_commands else ""
            self._record_node_launches(session, run, spec, launch_commands)
            run.endpoint_url = handle.endpoint_url
            self._transition(session, run, RunStatus.LAUNCHING)
            return

        handle = driver.attach(spec)
        if run.status in (RunStatus.LAUNCHING.value, RunStatus.WAITING_READY.value):
            if handle is None:
                self._fail(session, run, "gone", "container disappeared")
                return
            state = driver.state(handle)
            if state == DeploymentState.READY:
                # Adopt the endpoint from the now-ready handle. Some substrates
                # publish the reachable URL only once the workload is up (the
                # k8s operator sets status.endpoint when it goes Ready), so the
                # value captured at launch was a placeholder; attach() re-resolved
                # it above. Harmless where the endpoint was already final (ssh,
                # k8s deployment mode) — attach returns the same URL.
                if handle.endpoint_url:
                    run.endpoint_url = handle.endpoint_url
                # Exactly once, while the container is definitely alive: after
                # teardown there is nothing left to ask, and asking every tick
                # would put an ssh round-trip on the readiness poll.
                self._snapshot_environment(run, handle)
                self._transition(session, run, RunStatus.HEALTH_CHECK)
            elif state == DeploymentState.CRASHED:
                self._capture_failure(session, run, handle)
            else:
                # Two clocks: a cold image pull gets a generous budget, the
                # model load a tighter one from when its container started — so a
                # slow first pull no longer trips a false ready_timeout.
                timed_out, why, waiting = self._startup_timed_out(run, driver, handle)
                if timed_out:
                    self._capture_failure(
                        session, run, handle, failure_class="ready_timeout", error=why,
                    )
                else:
                    # Queued for cards, or no longer queued — either way say so
                    # before the status transition, so a run that has been
                    # waiting since 19:40 reads as waiting and not as a launch
                    # that has been LAUNCHING for three hours.
                    self._note_placement(session, run, waiting)
                    if run.status == RunStatus.LAUNCHING.value:
                        self._transition(session, run, RunStatus.WAITING_READY)
                # STARTING within budget → keep waiting

        elif run.status == RunStatus.HEALTH_CHECK.value:
            campaign = run.campaign
            ref = self.health.start(run.endpoint_url, campaign.served_model_name, {})
            outcome = self.health.poll(ref)
            self._record_result(session, run, "health", outcome)
            if outcome.status != EvalStatus.PASSED:
                self._fail(session, run, "health_check", outcome.error)
                return
            if run.kind == RunKind.POLICY_LAUNCH.value:
                # A delegated launch is FOR the policy, not for a benchmark:
                # healthy means hold it here until released. Benchmarks of it
                # are separate EXTERNAL runs pointed at this endpoint.
                self._transition(session, run, RunStatus.SERVING)
                return
            try:
                bench_ref = self.bench.start(
                    run.endpoint_url,
                    campaign.served_model_name,
                    self._bench_context(run),
                )
            except EvaluatorBusy as exc:
                # Quota/cordon — the config is fine, the platform is busy.
                # Stay in HEALTH_CHECK and try again next tick.
                logger.info("run %d waiting for the benchmark platform: %s", run.id, exc)
                return
            except EvaluatorRejected as exc:
                # The benchmark platform refused this endpoint (preflight) —
                # that is a verdict on the run, not a transient condition.
                self._fail(session, run, "bench_preflight", str(exc)[:2000])
                return
            run.llmbench_submission_id = bench_ref
            self._transition(session, run, RunStatus.BENCHING)

        elif run.status == RunStatus.BENCHING.value:
            outcome = self.bench.poll(run.llmbench_submission_id)
            if outcome.status == EvalStatus.RUNNING:
                return
            self._record_result(session, run, "llmbench", outcome)
            if outcome.status == EvalStatus.PASSED:
                self._finish(session, run, RunStatus.SUCCEEDED)
            else:
                # The engine may have died under load — save its log before
                # _fail's teardown removes the only copy.
                self._capture_engine_log(session, run)
                self._fail(session, run, _bench_failure_class(outcome), outcome.error)

        elif run.status == RunStatus.SERVING.value:
            # Held for a policy. Release turns into a clean SUCCEEDED (the run
            # did its job even though nothing benched under this id); a crash
            # is classified like any other so the policy can learn from it.
            if run.release_requested_at is not None:
                self._finish(session, run, RunStatus.SUCCEEDED)
                return
            if handle is None:
                self._fail(session, run, "gone", "engine container disappeared while serving")
                return
            if driver.state(handle) == DeploymentState.CRASHED:
                self._capture_failure(session, run, handle)

    def _advance_baseline(self, session: Session, run: Run) -> None:
        """Baseline canary: health-check then benchmark an already-running
        production service. No launch, no teardown."""
        campaign = run.campaign
        served_model = (
            next(
                (
                    s.get("served_model_name")
                    for s in (run.machine.baseline or {}).get("services", [])
                    if s.get("endpoint_url") == run.endpoint_url
                ),
                "",
            )
            if run.machine
            else ""
        ) or campaign.served_model_name

        if run.status == RunStatus.HEALTH_CHECK.value:
            ref = self.health.start(run.endpoint_url, served_model, {})
            outcome = self.health.poll(ref)
            if outcome.status != EvalStatus.PASSED and _looks_unreachable(outcome.error):
                # Nothing is listening yet. A production service that has just
                # been restored has its container up within seconds and its
                # weights loaded minutes later, so an immediate probe finds a
                # refused connection — and because a canary fails inside one
                # tick, all three attempts burn in twenty seconds against a
                # service that was about to be fine. Wait, as an experiment
                # waiting for readiness would.
                if not self._ready_timed_out(run):
                    logger.info(
                        "baseline run %d: %s not answering yet, waiting",
                        run.id, run.endpoint_url,
                    )
                    return
                self._record_result(session, run, "health", outcome)
                self._fail(
                    session, run, "baseline_unreachable",
                    f"nothing answering at {run.endpoint_url} after "
                    f"{self.settings.ready_timeout_minutes} min: {outcome.error}",
                )
                return
            self._record_result(session, run, "health", outcome)
            if outcome.status != EvalStatus.PASSED:
                # It answered, and answered badly — the handed-over service is
                # unhealthy, which is a fact worth knowing before the night.
                self._fail(session, run, "baseline_unhealthy", outcome.error)
                return
            try:
                bench_ref = self.bench.start(
                    run.endpoint_url, served_model, self._bench_context(run)
                )
            except EvaluatorBusy as exc:
                logger.info("baseline run %d waiting for the platform: %s", run.id, exc)
                return
            except EvaluatorRejected as exc:
                self._fail(session, run, "bench_preflight", str(exc)[:2000])
                return
            run.llmbench_submission_id = bench_ref
            self._transition(session, run, RunStatus.BENCHING)

        elif run.status == RunStatus.BENCHING.value:
            outcome = self.bench.poll(run.llmbench_submission_id)
            if outcome.status == EvalStatus.RUNNING:
                return
            self._record_result(session, run, "llmbench", outcome)
            if outcome.status == EvalStatus.PASSED:
                self._finish(session, run, RunStatus.SUCCEEDED)
            else:
                self._fail(session, run, _bench_failure_class(outcome), outcome.error)

    # --------------------------------------------------------------- enforce

    def _enforce_windows(self, session: Session) -> None:
        live_runs = session.scalars(
            select(Run).where(Run.status.not_in([s.value for s in TERMINAL_RUN_STATES]))
        ).all()
        for run in live_runs:
            window_end = _as_utc(run.campaign.window_end)
            if window_end is not None and _now() >= window_end:
                if run.llmbench_submission_id:
                    self.bench.cancel(run.llmbench_submission_id)
                self._finish(session, run, RunStatus.KILLED, error="window cutoff")

    # --------------------------------------------------------------- helpers

    def _bench_context(self, run: Run) -> dict:
        """Metadata for the benchmark platform: what config is under test and
        on what hardware (the latter drives card-normalized metrics there)."""
        campaign = run.campaign
        config = run.candidate.config
        stage = stage_of_run(run)
        config_summary = " ".join(f"{k}={v}" for k, v in sorted(config.items()))
        label = "BASELINE" if run.kind == RunKind.BASELINE.value else "autotune"
        if stage == VERIFY:
            label = "VERIFY"
        context: dict = {
            # Per stage, not per campaign: the whole point of the second stage
            # is that it is a different, more expensive benchmark.
            "benchmark_slug": _benchmark_slug(campaign, stage) or None,  # None -> default
            "description_summary": f"{label} run {run.id}: {config_summary}"[:100],
            "description_detail": (
                f"Automated tuning run from the LLM Autotune platform.\n\n"
                f"- campaign: {campaign.name} (#{campaign.id})\n"
                f"- engine: {campaign.engine}\n"
                f"- image: {campaign.image}\n"
                f"- model: {campaign.model_path}\n"
                f"- config: `{config}`\n"
                f"- launch command: `{run.launch_command}`\n"
            ),
        }
        if run.machine is not None:
            # Cards the CONFIG actually uses, not the whole box: the platform's
            # card-normalized metrics are only comparable across tp/dp/pp
            # variants if each is credited with the GPUs it occupies. A baseline
            # now carries production's real engine args, so it is counted the
            # same way as any candidate — no special case.
            cards = _cards_used(config)
            # `cards` is the config's WORLD size (tp×dp×pp), which is the whole
            # deployment for a single node and the total across the gang for a
            # multi-node one. LLMBench card-normalizes per machine, so the two
            # numbers it gets are the per-node width and the node count — for a
            # single-node run that is (cards, 1), exactly as before.
            session = Session.object_session(run)
            machine_count = len(nodes_of(session, run.id)) if session is not None else 1
            machine_count = max(1, machine_count)
            context["hardware"] = {
                "cards_per_machine": max(1, cards // machine_count)
                if cards else (run.machine.gpu_count or None),
                "machine_count": machine_count,
                "card_type": run.machine.gpu_type or None,
            }
        # Who the submission is listed under on LLMBench, and the way back: a
        # row on its leaderboard should lead to the campaign that produced it.
        session = Session.object_session(run)
        owner = None
        if session is not None and campaign.owner_id:
            owner = session.get(User, campaign.owner_id)
        context["contributor"] = f"autotune:{owner.username}" if owner else "autotune"
        ui = self.settings.public_ui_url.rstrip("/")
        if ui:
            context["source_url"] = f"{ui}/campaigns/{campaign.id}"
        return context

    def _node_assignments(self, session: Session, run: Run) -> list[NodeAssignment]:
        """The run's node plan, master first, rebuilt from `run_nodes`.

        Rebuilt rather than remembered: this is what a worker restart re-attaches
        with, so it has to come from the database alone. Falls back to the run's
        own master columns when the node table has nothing (a run created before
        the binding existed, or a test that inserts a Run directly with the
        mapper event disabled).
        """
        assignments: list[NodeAssignment] = []
        for node in nodes_of(session, run.id):
            machine = session.get(Machine, node.machine_id) if node.machine_id else None
            if machine is None:
                continue
            info = self._machine_info(machine)
            assignments.append(
                NodeAssignment(
                    machine=info,
                    gpu_indices=list(node.gpu_indices or []),
                    rank=node.rank,
                    container_name=node.container_name
                    or f"autotune-run-{run.id}"
                    + ("" if node.rank == 0 else f"-r{node.rank}"),
                    data_host=info.interior_host,
                )
            )
        if assignments:
            return assignments
        if run.machine is None:
            return []
        info = self._machine_info(run.machine)
        return [
            NodeAssignment(
                machine=info,
                gpu_indices=list(run.gpu_indices or []),
                rank=0,
                container_name=run.container_name or f"autotune-run-{run.id}",
                data_host=info.interior_host,
            )
        ]

    def _group_for_run(self, session: Session, run: Run) -> MachineGroup | None:
        if not run.node_group:
            return None
        return session.scalars(
            select(MachineGroup).where(MachineGroup.name == run.node_group)
        ).first()

    def _spec_for(self, session: Session, run: Run) -> LaunchSpec:
        campaign = run.campaign
        machine = run.machine
        assert machine is not None
        nodes = self._node_assignments(session, run)
        master_info = nodes[0].machine if nodes else self._machine_info(machine)
        # The group is the fabric authority: its env and mounts apply to every
        # rank, and its NCCL settings win over a campaign's because they describe
        # the interconnect the boxes are actually wired with.
        env = dict(campaign.extra_env or {})
        volumes = dict(campaign.extra_volumes or {})
        group = self._group_for_run(session, run)
        if group is not None:
            env.update(group.extra_env or {})
            env.update(group.nccl_env or {})
            volumes = merge_volumes(volumes, group.extra_volumes or {})
        if len(nodes) > 1:
            # A gang needs TWO process groups on the same fabric, and the second
            # one is easy to forget: torch's Gloo backend rendezvouses on the
            # socket interface, and with nothing set it resolves the container
            # hostname — which every box here maps to loopback in /etc/hosts
            # (`127.0.1.1 <hostname>`), so rank 0's Gloo store is unreachable and
            # the workers die with `connectFullMesh failed`. NCCL_SOCKET_IFNAME
            # already names the right NIC for the NCCL store; Gloo wants the
            # same answer, so copy it unless the operator set Gloo explicitly.
            ifname = str(env.get("NCCL_SOCKET_IFNAME") or "").strip()
            if ifname and not str(env.get("GLOO_SOCKET_IFNAME") or "").strip():
                env["GLOO_SOCKET_IFNAME"] = ifname
        dist_port = run.dist_port or 0
        return LaunchSpec(
            run_id=run.id,
            machine=master_info,
            engine=campaign.engine,
            image=campaign.image,
            model_path=campaign.model_path,
            served_model_name=campaign.served_model_name,
            engine_args=run.candidate.config,
            gpu_indices=list(nodes[0].gpu_indices) if nodes else list(run.gpu_indices or []),
            port=run.service_port or campaign.service_port,
            env=env,
            volumes=volumes,
            nodes=nodes,
            nnodes=max(1, len(nodes)),
            dist_port=dist_port,
            # Only a gang has a rendezvous. A single-node run with a leftover
            # dist_port must not be handed an address nothing is listening on.
            dist_init_addr=(
                f"{master_info.interior_host}:{dist_port}"
                if dist_port and len(nodes) > 1
                else ""
            ),
        )

    def _record_node_launches(
        self, session: Session, run: Run, spec: LaunchSpec, commands: list[str]
    ) -> None:
        """Write each rank's container name, endpoint and exact command onto its
        node row.

        `commands` aligns with `spec.nodes` sorted by rank by construction —
        that is the order `launch_nodes` started them in. The run's own
        `launch_command` stays the master's for every reader that predates
        multi-node; a gang's reproduction is node by node and lives here.
        """
        by_rank = {node.rank: node for node in nodes_of(session, run.id)}
        ordered = sorted(spec.nodes, key=lambda node: node.rank) if spec.nodes else []
        for command, node in zip(commands, ordered, strict=False):
            row = by_rank.get(node.rank)
            if row is None:
                continue
            row.container_name = node.container_name or row.container_name
            row.launch_command = command
            row.endpoint_url = f"http://{node.machine.host}:{spec.port}"

    def _transition(self, session: Session, run: Run, target: RunStatus) -> None:
        if not can_transition(run.status, target):
            raise RuntimeError(f"illegal transition {run.status} -> {target.value} (run {run.id})")
        self._event(
            session, "run_transition", campaign_id=run.campaign_id, run_id=run.id,
            payload={"from": run.status, "to": target.value},
        )
        run.status = target.value

    def _note_placement(self, session: Session, run: Run, waiting: str) -> None:
        """Keep `runs.waiting_since` true, and announce each edge once.

        Only the transitions are events: a run queued overnight would otherwise
        write one row per tick for eight hours, and the interesting facts are
        exactly two — the cluster started saying no, and it stopped.
        """
        if waiting:
            if run.waiting_since is None:
                run.waiting_since = _now()
                self._event(
                    session, "run_waiting_for_cluster",
                    campaign_id=run.campaign_id, run_id=run.id,
                    payload={"reason": waiting[:2000]},
                )
                logger.info("run %d is queued for cluster capacity: %s", run.id, waiting)
            return
        if run.waiting_since is not None:
            waited = _now() - _as_utc(run.waiting_since)
            run.waiting_since = None
            self._event(
                session, "run_placed",
                campaign_id=run.campaign_id, run_id=run.id,
                payload={"waited_seconds": int(waited.total_seconds())},
            )
            logger.info("run %d got its cards after %s queued", run.id, waited)

    def _finish(
        self, session: Session, run: Run, status: RunStatus, error: str = ""
    ) -> None:
        # Teardown on every terminal path — except runs whose "deployment" is
        # not ours to destroy: a baseline measures the handed-over production
        # service, an EXTERNAL run measures an endpoint a policy serves (or a
        # delegated engine that its own run owns).
        cleaned = True
        if run.kind not in (RunKind.BASELINE.value, RunKind.EXTERNAL.value):
            # Save the engine log while the container is still up — on success
            # too, not only failures — so every run's log is viewable after the
            # fact. Idempotent (skips if a crash already captured it) and
            # best-effort (never fails the run).
            self._capture_engine_log(session, run)
            cleaned = self._begin_teardown(session, run)
        if error:
            run.error = error
        run.finished_at = _now()
        self._transition(session, run, status)
        # Free the machine only once nothing is left holding it — including a
        # container still tearing down (its cards stay reserved until the
        # janitor confirms it gone, so the next run never lands on top of it).
        # With sharing, this releases only when the LAST tenant leaves; a live
        # policy session counts as a tenant the run table cannot see. A gang
        # offers back every member, not just the master.
        if cleaned:
            self._release_run_machines(session, run)

    def _begin_teardown(self, session: Session, run: Run) -> bool:
        """Start tearing a run's container down; return whether it is already
        gone. Graceful first — a SIGTERM so the engine releases its cards and
        NCCL cleanly — and if the container does not vanish at once the run is
        marked teardown_pending so the janitor finishes the job, rather than the
        cards being freed on the optimistic assumption it died."""
        try:
            spec = self._spec_for(session, run)
            driver = self._driver_for(spec.machine)
            handle = driver.attach(spec)
        except Exception:
            logger.exception("teardown setup for run %d failed", run.id)
            run.teardown_pending = True  # cannot reach it; let the janitor retry
            return False
        if handle is None:
            return True  # nothing running under this run's name
        try:
            driver.request_stop(handle)
            if driver.is_gone(handle):
                return True
        except Exception:
            logger.exception("graceful stop for run %d failed", run.id)
        run.teardown_pending = True
        return False

    def _fail(self, session: Session, run: Run, failure_class: str, error: str) -> None:
        run.failure_class = failure_class
        self._finish(session, run, RunStatus.FAILED, error=error)
        self._requeue_if_infrastructure(session, run)

    def _requeue_if_infrastructure(self, session: Session, run: Run) -> None:
        """Give a candidate its slot back when the failure was not its fault.

        Losing a config to a transient ssh timeout means the night silently
        tests less than it was asked to — and nobody is awake to press retry.
        Bounded, so a genuinely unlaunchable config cannot loop forever.
        """
        if run.kind == RunKind.BASELINE.value:
            return  # a canary is not a candidate; failing it is the signal
        if run.kind in (RunKind.POLICY_LAUNCH.value, RunKind.EXTERNAL.value):
            return  # the policy decides whether to retry its own requests
        if not is_infrastructure(run.failure_class):
            return
        attempts = (
            session.scalars(
                select(func.count(Run.id)).where(
                    Run.candidate_id == run.candidate_id,
                    # A run we withdrew ourselves (a pause giving back a cluster
                    # queue slot) is not an attempt: nothing was tried and
                    # nothing was learned, so it must not spend the budget that
                    # exists to stop genuinely-unlaunchable configs looping.
                    Run.failure_class != RELEASED,
                )
            ).first()
            or 0
        )
        if attempts >= MAX_ATTEMPTS_PER_CANDIDATE:
            self._event(
                session, "candidate_given_up",
                campaign_id=run.campaign_id, run_id=run.id,
                payload={"attempts": attempts, "failure_class": run.failure_class},
            )
            return
        run.candidate.status = CandidateStatus.VALID.value
        self._event(
            session, "candidate_requeued",
            campaign_id=run.campaign_id, run_id=run.id,
            payload={"attempt": attempts, "failure_class": run.failure_class},
        )
        logger.info(
            "re-queued candidate %d after %s (attempt %d/%d)",
            run.candidate_id, run.failure_class, attempts, MAX_ATTEMPTS_PER_CANDIDATE,
        )

    def _capture_failure(
        self,
        session: Session,
        run: Run,
        handle,
        failure_class: str | None = None,
        error: str = "container crashed; see log",
    ) -> None:
        driver = self._driver_for(handle.machine) if handle is not None else self.driver
        log_text = ""
        try:
            log_text = driver.logs(handle, tail=400)
        except Exception:
            logger.exception("log capture for run %d failed", run.id)
        self._save_run_log(run, log_text)
        resolved_class = failure_class or classify_failure(log_text)
        # A container that never started leaves no log to classify (a bad model
        # mount, an unpullable image, an unschedulable pod). The substrate still
        # knows why, so ask it and surface that real reason instead of the
        # generic "see log" / "not ready after N min".
        if handle is not None and (not log_text.strip() or resolved_class == "unknown"):
            reason = driver.failure_reason(handle)
            if reason is not None:
                reason_class, reason_message = reason
                # A ready_timeout is the absence of information, not
                # information — when the substrate can name what actually
                # wedged the run, that name wins. Campaign 38 is why: a node
                # was cordoned mid-campaign, four pods sat Pending to the
                # 30-min timeout, and the explicit "ready_timeout" here kept
                # the substrate's "unschedulable" as message-only. The class
                # difference matters twice over: ready_timeout is config-
                # induced (taught the optimizer that healthy configs crash),
                # unschedulable is infrastructure (blameless, bounded retry).
                if failure_class is None or failure_class == "ready_timeout":
                    resolved_class = reason_class
                error = reason_message
        if resolved_class == "unknown" and handle is not None:
            # Logs said nothing — ask the substrate how the container died.
            exit_code, oom_killed = driver.exit_info(handle)
            exit_class = classify_exit(exit_code, oom_killed)
            if exit_class:
                resolved_class = exit_class
            if exit_code is not None:
                error = f"{error} (exit code {exit_code})"
        self._fail(session, run, resolved_class, error)

    def _capture_engine_log(self, session: Session, run: Run) -> None:
        """Save the engine container's log before a benchmark failure tears it
        down — the one moment its stderr is still reachable.

        A replay can fail because the *served engine* died under load: run 216
        crashed 49 requests into a replay and reported 6.5% uptime, and the OOM
        traceback that explains it lives only in the container's log. `_fail`
        removes the container, so grabbing the tail here — not after — is the
        difference between a diagnosable failure and an inferred one.

        Best-effort throughout: the container is the run's own (never a
        baseline's production service), it is not re-captured if a crash during
        launch already saved it, and a log we cannot read must never turn a real
        verdict into a supervisor crash.
        """
        if run.kind in (RunKind.BASELINE.value, RunKind.EXTERNAL.value) or run.log_path:
            return  # no container of this run's own to read
        log_text = ""
        try:
            spec = self._spec_for(session, run)
            driver = self._driver_for(spec.machine)
            handle = driver.attach(spec)
            if handle is not None:
                log_text = driver.logs(handle, tail=400)
        except Exception:
            logger.exception("engine log capture for run %d failed", run.id)
        self._save_run_log(run, log_text)

    def _save_run_log(self, run: Run, log_text: str) -> None:
        """Persist a captured container log and point the run at it. No-op on an
        empty log, so a run whose log we could not read keeps log_path unset
        rather than pointing at an empty file."""
        if not log_text:
            return
        os.makedirs(self.settings.run_log_dir, exist_ok=True)
        log_path = os.path.join(self.settings.run_log_dir, f"run-{run.id}.log")
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(log_text)
        run.log_path = log_path

    def _snapshot_environment(self, run: Run, handle) -> None:
        """Record what this run is actually about to be measured on.

        Best-effort by design: provenance that costs a run is a bad trade, and
        a partial snapshot (digest but no version) is still enough to tell two
        nights apart. The GPU model comes from the machine row rather than the
        container, so a box re-labelled later cannot rewrite what it was.
        """
        snapshot: dict = {}
        try:
            snapshot = dict(self._driver_for(handle.machine).environment(handle))
        except Exception:
            logger.exception("environment snapshot for run %d failed", run.id)
        if run.machine is not None and run.machine.gpu_type:
            snapshot.setdefault("machine_gpu_type", run.machine.gpu_type)
        # A definitive card type for this run: the card it ACTUALLY landed on
        # (from the driver's node read) wins over the machine's declared type,
        # which can be stale or, for a node-pool machine, one of several. The
        # leaderboard reads this to refuse ranking an H100 run against an A100
        # baseline. Falls back to the machine's type when the substrate cannot
        # say (a bare-metal box, or an unreadable node).
        card_type = snapshot.get("card_type") or (
            run.machine.gpu_type if run.machine is not None else ""
        )
        if card_type:
            snapshot["card_type"] = card_type
        snapshot["gpu_indices"] = list(run.gpu_indices or [])
        run.env_snapshot = snapshot

    def _ready_timed_out(self, run: Run) -> bool:
        if run.started_at is None:
            return False
        started = _as_utc(run.started_at)
        return _now() - started > timedelta(minutes=self.settings.ready_timeout_minutes)

    def _startup_timed_out(self, run: Run, driver, handle) -> tuple[bool, str, str]:
        """Has a coming-up run overrun its budget — and if so, which one.

        Two clocks (see Settings.image_pull_timeout_minutes): while the pod is
        still scheduling or pulling its image, the generous pull budget from
        launch; once a container is running, the tight ready budget from when it
        started. A substrate that cannot tell the phases apart ('unknown') keeps
        the old single ready-timeout.

        Returns (timed_out, why, waiting): `waiting` is a human reason the run
        is QUEUED rather than late — set only for placement the substrate calls
        transient, which never times out at all."""
        if run.started_at is None:
            return (False, "", "")
        started = _as_utc(run.started_at)
        now = _now()
        try:
            status = driver.startup_status(handle)
        except Exception:
            status = {"phase": "unknown", "running_since": None}
        ready_limit = timedelta(minutes=self.settings.ready_timeout_minutes)
        pull_limit = timedelta(minutes=self.settings.image_pull_timeout_minutes)

        running_since = _parse_rfc3339(status.get("running_since"))
        if status.get("phase") == "running" and running_since is not None:
            if now - running_since > ready_limit:
                return (True, f"engine not ready {self.settings.ready_timeout_minutes} "
                              "min after its container started", "")
            return (False, "", "")
        if status.get("phase") == "pulling":
            if now - started > pull_limit:
                # Prefer the substrate's own reason (e.g. the unschedulable pod's
                # "0/N nodes available: …") over a generic timeout — a pod stuck
                # this long has a concrete cause worth surfacing.
                try:
                    reason = driver.failure_reason(handle)
                except Exception:
                    reason = None
                if reason is not None:
                    # A QUEUED run is not a late one. On a cluster we share with
                    # production, "no cards yet" is the scheduler working
                    # correctly, and the run schedules the moment a node frees
                    # what it asked for. Failing it here is what used to spend a
                    # candidate's whole retry budget on a busy evening and then
                    # drop it from the grid. So the pull timeout stops being a
                    # deadline for these and becomes the point where we start
                    # SAYING what is happening; the machine's lease is the real
                    # bound, and drains the run like any other when it ends.
                    if is_transient_placement(reason[0]):
                        return (False, "", reason[1])
                    return (True, reason[1], "")
                return (True, f"pod still not running {self.settings.image_pull_timeout_minutes} "
                              "min after launch (scheduling / image pull)", "")
            return (False, "", "")
        # Unknown substrate: the original single-timeout behaviour, unchanged.
        if now - started > ready_limit:
            return (True, f"not ready after {self.settings.ready_timeout_minutes} min", "")
        return (False, "", "")

    def _record_result(self, session: Session, run: Run, source: str, outcome) -> None:
        result = Result(
            run_id=run.id,
            source=source,
            passed=outcome.status == EvalStatus.PASSED,
            score=outcome.metrics.get("score_total"),
            metrics=outcome.metrics,
            raw=outcome.raw,
        )
        # Resolve the objective now, against the objective this campaign had
        # when the run was measured. Only the benchmark reports the metrics an
        # objective names; a health probe has nothing to score.
        #
        # Which objective depends on the STAGE: a verification run submits to a
        # different benchmark, whose metric names the screening objective does
        # not contain. Scoring it against the campaign's own objective yields
        # None, which every reader downstream takes to mean "this run measured
        # nothing" — the expensive stage would produce no usable evidence at all.
        if source == "llmbench":
            stored = summarize(
                _stage_objective(run.campaign, stage_of_run(run)), outcome.metrics
            ).as_dict()
            result.objective_value = stored["objective_value"]
            result.feasible = stored["feasible"]
            result.constraints = stored["constraints"]
            result.breaches = stored["breaches"]
            self._check_dataset(session, run, outcome.metrics)
            if run.policy_session_id is not None:
                self._record_policy_trial(session, run, stored, outcome.metrics)
        session.add(result)

    def _record_policy_trial(
        self, session: Session, run: Run, stored: dict, metrics: dict
    ) -> None:
        """A platform-measured benchmark becomes a trial with `source:
        "platform"` — the observability stream the UI shows live and the
        cross-night history a policy warm-starts from (docs/api/policy-contract
        .md). The policy self-reports only its OWN benchmarks; these it does not,
        so if the platform does not record them the trial ledger stays empty.

        Idempotent on the run: `_record_result` runs once per completion, but a
        redelivery must not double-count."""
        key = f"platform-run-{run.id}"
        already = session.execute(
            select(PolicyTrial.id).where(PolicyTrial.idempotency_key == key)
        ).first()
        if already is not None:
            return
        candidate = session.get(Candidate, run.candidate_id) if run.candidate_id else None
        seq = session.execute(
            select(func.count(PolicyTrial.id)).where(
                PolicyTrial.session_id == run.policy_session_id
            )
        ).scalar_one()
        session.add(
            PolicyTrial(
                session_id=run.policy_session_id,
                campaign_id=run.campaign_id,
                seq=seq + 1,
                config=(candidate.config if candidate else {}),
                config_hash=(candidate.config_hash if candidate else ""),
                source="platform",
                run_id=run.id,
                reported_metrics=metrics,
                reported_objective_value=stored["objective_value"],
                reported_feasible=stored["feasible"],
                idempotency_key=key,
            )
        )

    def _check_dataset(self, session: Session, run: Run, metrics: dict) -> None:
        """Verify the run replayed the dataset its campaign pinned.

        Owning the profile is what makes the pin hold; reading it back off
        every result is what makes it provable — and it is the only check that
        survives a mistake on either side, including a benchmark quietly
        pointed at a different profile.

        A mismatch does not fail the run. The measurement is real and stays in
        the store; it simply cannot be ranked against numbers taken with a
        different instrument, which is a question for whoever reads the
        leaderboard, not a reason to throw an hour of GPU time away.
        """
        found = pinning.mismatch(run.campaign, metrics)
        if found is None:
            return
        build_id, sha = found
        self._event(
            session, "dataset_mismatch", campaign_id=run.campaign_id, run_id=run.id,
            payload={
                "profile": run.campaign.dataset_profile,
                "pinned": run.campaign.dataset_build_id,
                "replayed": build_id,
                "replayed_sha256": sha,
            },
        )
        logger.warning(
            "run %d replayed %s but campaign %d pinned %s — not comparable",
            run.id, build_id or sha, run.campaign_id, run.campaign.dataset_build_id,
        )
    def _usable_machines(self, session: Session, campaign: Campaign) -> list[Machine]:
        """Machines this campaign is allowed to place work on.

        Campaigns pin machines by name because they are tied to a model, an
        image and a GPU layout — running one on an arbitrary free box (as
        happened live: an sglang campaign landed on the CPU-only platform host)
        wastes a slot at best.

        RESERVED is included: it means "some run is live here", which with
        machine sharing no longer implies the machine is full. AWAY does mean
        hands off — it has not been handed over to the platform.

        A DRAINING machine is excluded even though it is still ours: its lease
        holder has been told no further work will start, and a placement made
        after that promise is a placement that has to be killed to keep it. A
        quarantined machine (needs_attention) is excluded too: the platform
        could not confirm it clean, so it is held for a human rather than handed
        to the next run on top of whatever is still stuck on it.
        """
        query = select(Machine).where(
            Machine.state != MachineState.AWAY.value,
            Machine.needs_attention.is_(False),
        )
        allowed = campaign.machine_names or []
        if allowed:
            query = query.where(Machine.name.in_(allowed))
        machines = session.scalars(query.order_by(Machine.id)).all()
        # A machine hosting a live policy session is that session's exclusively
        # for the night — accepts_new_work cannot see this (it has no DB
        # session), so it is asked here. The session's own campaign gets the
        # machine back through the session engine, not through this list.
        # The exception is a policy campaign that shares: its session takes a
        # card slice next to the tenants already there, and the session engine
        # (`_allocation_on`) decides whether there is room. Classic campaigns
        # keep the exclusion — the classic scheduler only sees a session's RUNS,
        # not the idle cards it has reserved, and would place on top of them.
        joins = bool(campaign.share_machine and campaign.policy_id is not None)
        return [
            m
            for m in machines
            if accepts_new_work(m) and (joins or not machine_has_live_session(session, m))
        ]
    def _startable_slots(self, session: Session, campaign: Campaign) -> int:
        """Upper bound on runs this campaign could start on the next tick.

        One slot per free card — the most a tp=1 candidate could occupy. Cards
        are the only capacity signal available before a config exists, and
        over-estimating here just means a slightly deeper queue, never an
        overcommitted machine: placement is still decided by the packer.
        """
        group = self._group_for_campaign(session, campaign)
        if group is not None:
            # A gang runs on the whole group or not at all, so the group's idle
            # capacity is the answer — and it is 0 while any member is busy,
            # which is exactly what `_start_gang` will find.
            if not self._group_is_idle(session, group):
                return 0
            return sum(m.gpu_count for _, m in members_by_rank(session, group) if m.gpu_count > 0)
        slots = 0
        for machine in self._usable_machines(session, campaign):
            if machine.baseline_status != BaselineStatus.CLEARED.value:
                continue
            # A member of a live gang contributes no single-node slots while the
            # gang runs — the same closure `_start_one` applies, so the proposal
            # budget and the packer agree on the capacity.
            if machine_hosts_live_gang(session, machine.id):
                continue
            live = self._live_runs_on(session, machine)
            if live and not self._may_share(session, campaign, live):
                continue
            if machine.gpu_count <= 0:
                slots += 0 if live else 1  # CPU-only box hosts exactly one run
                continue
            taken = {
                index
                for node in self._live_nodes_on(session, machine)
                for index in (node.gpu_indices or [])
            }
            slots += len(free_indices(machine.gpu_count, taken))
        return slots

    def _group_is_idle(self, session: Session, group: MachineGroup) -> bool:
        """Could this group host a gang RIGHT NOW?

        Every member ours, handed over, un-quarantined, with no run and no
        policy session on it. A gang is exclusive — its ranks share an
        interconnect and a host — so "enough free cards" is not the bar; idle is.
        """
        for _, machine in members_by_rank(session, group):
            if not accepts_new_work(machine) or machine.needs_attention:
                return False
            if machine.baseline_status != BaselineStatus.CLEARED.value:
                return False
            if machine.gpu_count <= 0:
                return False
            if machine_has_live_session(session, machine):
                return False
            if self._live_runs_on(session, machine) or self._teardown_pending_on(session, machine):
                return False
        return True

    def _campaign_nodes(self, session: Session, campaign: Campaign) -> int:
        """How many machines this campaign's runs span. 1 unless it pins a group."""
        group = self._group_for_campaign(session, campaign)
        if group is None:
            return 1
        return max(1, len(members_by_rank(session, group)))

    def _group_for_campaign(self, session: Session, campaign: Campaign) -> MachineGroup | None:
        if not campaign.node_group:
            return None
        return session.scalars(
            select(MachineGroup).where(MachineGroup.name == campaign.node_group)
        ).first()

    def _validation_gpu_count(self, session: Session, campaign: Campaign) -> int:
        """The GPUs one candidate may ask for and still be placeable.

        Validating against the whole fleet is wrong the moment the fleet is
        heterogeneous: a campaign pinned to a 4-card box would accept a tp=8
        candidate because some OTHER machine has eight cards. The candidate is
        then marked valid, never fits anywhere the campaign is allowed to run,
        and the scheduler retries it forever — the campaign cannot even finish,
        because a pending candidate keeps it out of DONE.

        For a node group this is the group's TOTAL: `tp=16` across two 8-card
        boxes is exactly the deployment the group exists for, and judging it
        against one box would reject it. The per-node split is then checked
        separately (`_check_multi_node_shape`), because "fits in total" and
        "divides evenly" are different questions.
        """
        group = self._group_for_campaign(session, campaign)
        if group is not None:
            counts = [m.gpu_count for _, m in members_by_rank(session, group) if m.gpu_count > 0]
            if counts:
                return min(counts) * len(counts)
            return 0
        machines = self._usable_machines(session, campaign)
        counts = [m.gpu_count for m in machines if m.gpu_count > 0]
        if counts:
            return max(counts)
        # No pinned machine reports any cards (a CPU-only test host, or a fleet
        # not described yet). validate_config treats <= 0 as "capacity unknown"
        # and skips the fit check rather than rejecting everything.
        return 0 if machines else 8

    def _window_allows_new_run(self, campaign: Campaign) -> bool:
        return window_allows_new_run(campaign, self.settings.default_max_run_minutes)

    def _window_allows_stage(self, campaign: Campaign, stage: str) -> bool:
        return window_allows_run_of(
            campaign,
            _stage_max_run_minutes(campaign, stage, self.settings.default_max_run_minutes),
        )

    def _maybe_finish_campaign(self, session: Session, campaign: Campaign) -> None:
        pending = session.scalars(
            select(Candidate).where(
                Candidate.campaign_id == campaign.id,
                Candidate.status.in_(
                    [CandidateStatus.PENDING.value, CandidateStatus.VALID.value]
                ),
            ).limit(1)
        ).first()
        live = session.scalars(
            select(Run).where(
                Run.campaign_id == campaign.id,
                Run.status.not_in([s.value for s in TERMINAL_RUN_STATES]),
            ).limit(1)
        ).first()
        if pending is None and live is None:
            # The search is out of ideas — but the leader may only be leading
            # by noise. Queue repeats of the best candidates before calling it.
            if self._schedule_confirmations(session, campaign):
                return
            # A campaign that pins its dataset has nothing to verify against
            # until the pin lands. Holding here rather than finishing is the
            # point: the build is seconds to minutes away, and calling the
            # campaign done would throw away the expensive stage entirely.
            if self._waiting_on_dataset(campaign):
                return
            # Cheap evidence is in and settled; now spend the expensive
            # benchmark on the few candidates that earned it. Deliberately
            # after confirmation: a config leading on one lucky sample would
            # otherwise consume a slot that costs an hour rather than minutes.
            if self._schedule_verification(session, campaign):
                return
            campaign.status = CampaignStatus.DONE.value
            self._event(session, "campaign_done", campaign_id=campaign.id)

    def _waiting_on_dataset(self, campaign: Campaign) -> bool:
        """Is a stage that will replay still waiting to learn what it replays?

        Not staged-only any more: when the single benchmark IS the replay, the
        primary stage waits here too, so its first run does not measure a
        different slice of traffic than its last. True while the build is in
        flight; False once it has pinned (to the fresh build, or an older one
        when the build failed) or concluded no build is coming — a campaign must
        not sit out its window waiting for a dataset that has already said no.
        """
        if not pinning.uses_pinning(campaign):
            return False
        if pinning.is_pinned(campaign):
            return False
        return campaign.dataset_policy_applied != pinning.APPLIED_UNAVAILABLE

    # --------------------------------------------------------- confirmation

    def _ranked_screen_configs(
        self, session: Session, campaign: Campaign
    ) -> list[tuple[str, list[Run], float]]:
        """Configurations this campaign has screened, best first.

        Screening runs only. A verification run measures the same config
        against a different benchmark, so its objective value is on another
        scale entirely — averaging the two would produce a number that means
        nothing, and comparing configs by it would rank whichever ones happened
        to reach the second stage.
        """
        runs = [
            run
            for run in session.scalars(
                select(Run).where(
                    Run.campaign_id == campaign.id,
                    Run.kind == RunKind.EXPERIMENT.value,
                )
            ).all()
            # The baseline is measured on the same benchmark but is not a search
            # point: it must not take a shortlist slot, nor be ranked against the
            # configs it exists to be the reference for.
            if stage_of(run.candidate) == SCREEN and not is_baseline_candidate(run.candidate)
        ]
        if not runs:
            return []
        results = {
            result.run_id: result
            for result in session.scalars(
                select(Result)
                .where(
                    Result.run_id.in_([r.id for r in runs]),
                    Result.source == "llmbench",
                )
                .order_by(Result.id)
            ).all()
        }

        by_config: dict[str, list[Run]] = {}
        for run in runs:
            by_config.setdefault(run.candidate.config_hash, []).append(run)

        ranked: list[tuple[str, list[Run], float]] = []
        for config_hash, group in by_config.items():
            values = [
                summary.objective_value
                for summary in (
                    summary_of(results.get(run.id), campaign.objective)
                    for run in group
                    if run.status == RunStatus.SUCCEEDED.value
                )
                if summary.feasible and summary.objective_value is not None
            ]
            if values:
                # Rank on the mean of what we have. A config leading on one
                # lucky sample is exactly what this exists to catch, so it must
                # not be ranked on its best sample.
                ranked.append((config_hash, group, sum(values) / len(values)))
        ranked.sort(key=lambda row: sort_key(row[2], campaign.objective))
        return ranked

    def _schedule_confirmations(self, session: Session, campaign: Campaign) -> bool:
        """Re-run the best candidates so a winner is a measurement, not a draw.

        One benchmark per config is the weakest part of a night's evidence.
        Measurement noise on this rig is ~0.25%, which is small — but node-24
        also produced a config that passed every functional check on one run
        and regressed on another at the same settings. A single sample cannot
        tell "fastest" from "fastest that night", and promoting the second is
        how a tuner loses trust.

        Returns True when it queued work, which keeps the campaign ACTIVE for
        another pass. Bounded by construction: attempts are counted from runs
        that already exist, so once a config has `confirm_repeats` of them
        nothing further is created and the campaign finishes.
        """
        if campaign.confirm_top_k <= 0:
            return False
        target = max(1, campaign.confirm_repeats)
        ranked = self._ranked_screen_configs(session, campaign)
        if not ranked:
            return False

        created = 0
        for config_hash, group, value in ranked[: campaign.confirm_top_k]:
            # Every attempt counts, not just the successful ones: a config
            # whose repeats keep failing must stop being retried, and the
            # infrastructure retry path already covers blameless failures.
            missing = target - len(group)
            if missing <= 0:
                continue
            root = min(run.candidate_id for run in group)
            source = session.get(Candidate, root)
            for _ in range(missing):
                session.add(
                    Candidate(
                        campaign_id=campaign.id,
                        config=source.config,
                        # Same hash on purpose: this is the same point, so a
                        # search must keep reading it as already explored.
                        config_hash=config_hash,
                        status=CandidateStatus.VALID.value,
                        kind=CandidateKind.CONFIRMATION.value,
                        repeat_of=root,
                    )
                )
                created += 1
            self._event(
                session,
                "confirmation_scheduled",
                campaign_id=campaign.id,
                payload={
                    "config": source.config,
                    "repeats": missing,
                    "measured_so_far": len(group),
                    "mean_objective": value,
                },
            )
        if created:
            logger.info(
                "queued %d confirmation run(s) for campaign %d", created, campaign.id
            )
        return created > 0

    # --------------------------------------------------------- verification

    def _schedule_verification(self, session: Session, campaign: Campaign) -> bool:
        """Send the best screened configs to the expensive benchmark.

        A synthetic sweep with random tokens cannot see prefix cache reuse, and
        real traffic is 16-64K-token prompts against a service whose cache hit
        rate decides most of its throughput. So the sweep's ranking is a
        shortlist, not an answer: this re-measures the top of it against
        replayed production requests, and that measurement is what the report
        crowns.

        One verification per config, ever. Not because one measurement is
        enough — it is the same single-sample weakness confirmation exists to
        fix — but because a repeat costs the better part of an hour, and a
        campaign that silently spent a second night on it would be worse than
        one that says plainly what it measured. Anyone wanting more raises
        `verify_top_k` and reads the spread across configs.
        """
        if not is_staged(campaign):
            return False
        if self._waiting_on_dataset(campaign):
            return False
        ranked = self._ranked_screen_configs(session, campaign)
        if not ranked:
            return False

        already = set(
            session.scalars(
                select(Candidate.config_hash).where(
                    Candidate.campaign_id == campaign.id,
                    Candidate.kind == CandidateKind.VERIFICATION.value,
                )
            ).all()
        )
        created = 0
        for config_hash, group, value in ranked[: campaign.verify_top_k]:
            if config_hash in already:
                continue
            root = min(run.candidate_id for run in group)
            source = session.get(Candidate, root)
            session.add(
                Candidate(
                    campaign_id=campaign.id,
                    config=source.config,
                    # Same hash as the point it verifies, for the same reason a
                    # confirmation carries it: this is not new territory.
                    config_hash=config_hash,
                    status=CandidateStatus.VALID.value,
                    kind=CandidateKind.VERIFICATION.value,
                    repeat_of=root,
                )
            )
            created += 1
            self._event(
                session,
                "verification_scheduled",
                campaign_id=campaign.id,
                payload={
                    "config": source.config,
                    "benchmark": campaign.verify_benchmark_slug,
                    "screened_over": len(group),
                    "mean_screen_objective": value,
                },
            )
        # And the baseline is verified too — always, not as part of the
        # shortlist but as the reference the verify stage is ranked against.
        # Without it the expensive stage has no drift-robust anchor, which is
        # the whole reason a second stage measures real traffic at all.
        if campaign.run_baseline_canary and not self._has_baseline_candidate(
            session, campaign, VERIFY
        ):
            baseline = self._campaign_baseline(session, campaign)
            if baseline is not None:
                self._add_baseline_candidate(
                    session, campaign, baseline, CandidateKind.VERIFICATION.value
                )
                created += 1
        if created:
            logger.info(
                "queued %d verification run(s) for campaign %d against %s",
                created, campaign.id, campaign.verify_benchmark_slug,
            )
        return created > 0

    def _event(self, session: Session, kind: str, **kwargs) -> None:
        payload = kwargs.pop("payload", {})
        session.add(Event(actor="worker", kind=kind, payload=payload, **kwargs))
