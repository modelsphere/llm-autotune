"""Managed campaigns: how a plugin runs its work on the platform.

A plugin that needs something measured (one config on one machine, or a
policy searching a space) does not build campaign rows or drive the
scheduler. It hands the platform a campaign spec, the same body
`POST /api/campaigns` takes and a campaign's YAML export holds, and the
platform creates the campaign by the same rules as any other, records who
owns it, and runs it like any other: the machine queue, leases, windows,
benchmarks and results are the platform's.

What the plugin keeps is the campaign id. It reads progress from the
platform's own rows (runs, results, the campaign's status), and changes the
campaign only through the services here: start it (`start_managed_campaign`),
keep its window open (`extend_managed_campaign`), give it a dataset build
(`pin_managed_dataset`), and close it (`stop_managed_campaign`). Each refuses
a campaign the plugin does not own.

The owner (plugin, label, link) is what keeps these campaigns from crowding
the campaign list: the list hides owned campaigns unless asked, and a
managed campaign's page links back to the page it belongs to.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app import plugins
from app.campaign_rules import campaign_errors, read_recipe
from app.control.orchestrator.lifecycle import as_utc
from app.datasets.profiles import DatasetBuild
from app.db.models import (
    TERMINAL_RUN_STATES,
    TERMINAL_SESSION_STATES,
    Campaign,
    CampaignOwner,
    CampaignStatus,
    Event,
    PolicySession,
    Run,
)
from app.schemas.core import CampaignCreate


@dataclass(frozen=True)
class Owner:
    """Who runs a managed campaign, and what for."""

    plugin: str
    # What the campaign is for, as the owner names it ("Submission #42").
    label: str = ""
    # The owner's page for it, an app path ("/submissions/42").
    link: str = ""


class ManagedCampaignRefused(ValueError):
    """The spec breaks a rule every campaign must satisfy; the message says
    which. Nothing was created."""


# The workload descriptions that become benchmarks on LLMBench when a person
# creates a campaign. A managed campaign names its benchmarks by slug: making
# one is a call to LLMBench, which does not belong in a worker tick.
_NOT_FOR_MANAGED = ("benchmark_spec", "verify_benchmark_spec")
# Read into `recipe` at save, never stored.
_CREATE_ONLY = ("recipe_text",)


def create_managed_campaign(
    session: Session,
    *,
    owner: Owner,
    owner_id: int,
    spec: CampaignCreate | dict[str, Any],
    start_for: timedelta | None = None,
    at: datetime | None = None,
) -> Campaign:
    """Create a campaign that `owner.plugin` runs, by the rules every campaign
    follows, and record its owner. Returns the campaign, flushed (it has an id)
    but not committed: it commits with the caller's transaction.

    `start_for` starts it now and holds it open that long, as Force start
    does; without it the campaign is created the way the API creates one
    (DRAFT, or SCHEDULED when it carries a nightly schedule).

    Raises ManagedCampaignRefused when the spec breaks a rule, and
    ExtensionRefused when a plugin refuses the spec's `extensions`."""
    body = spec if isinstance(spec, CampaignCreate) else CampaignCreate.model_validate(spec)
    given = [f for f in _NOT_FOR_MANAGED if getattr(body, f) is not None]
    if given:
        raise ManagedCampaignRefused(
            f"{', '.join(given)}: a managed campaign names its benchmarks by slug"
        )
    problem = read_recipe(body)
    if problem:
        raise ManagedCampaignRefused(problem)
    errors = campaign_errors(session, body)
    if errors:
        raise ManagedCampaignRefused("; ".join(errors))

    from app.control.orchestrator import schedule as sched

    campaign = Campaign(
        owner_id=owner_id,
        **body.model_dump(exclude={"extensions", *_NOT_FOR_MANAGED, *_CREATE_ONLY}),
    )
    if start_for is not None:
        moment = at or datetime.now(UTC)
        campaign.status = CampaignStatus.ACTIVE.value
        campaign.window_start = moment
        campaign.window_end = moment + start_for
        campaign.override_until = moment + start_for
    elif sched.from_campaign(campaign) is not None:
        campaign.status = CampaignStatus.SCHEDULED.value
    session.add(campaign)
    session.flush()
    plugins.create_campaign_extensions(session, campaign, body.extensions)
    session.add(
        CampaignOwner(
            campaign_id=campaign.id,
            plugin=owner.plugin,
            label=owner.label[:200],
            link=owner.link[:300],
        )
    )
    session.add(
        Event(
            actor=f"plugin:{owner.plugin}",
            kind="campaign_created",
            campaign_id=campaign.id,
            payload={"name": campaign.name, "owner": owner.plugin, "label": owner.label},
        )
    )
    session.flush()
    return campaign


def _check_owner(session: Session, campaign: Campaign, plugin: str) -> None:
    owner = session.get(CampaignOwner, campaign.id)
    if owner is None or owner.plugin != plugin:
        raise ManagedCampaignRefused(
            f"campaign {campaign.id} is not run by plugin {plugin!r}"
        )


def start_managed_campaign(
    session: Session,
    campaign: Campaign,
    *,
    plugin: str,
    start_for: timedelta,
    machine_names: list[str] | None = None,
    at: datetime | None = None,
) -> None:
    """Start a managed campaign that has not started yet, now, and hold it
    open for `start_for`, as Force start does. `machine_names` pins it to
    those machines first (a plugin that hands out machines itself, one
    campaign at a time). Raises ManagedCampaignRefused for a campaign the
    plugin does not own, or one already started or finished."""
    _check_owner(session, campaign, plugin)
    if campaign.status not in (CampaignStatus.DRAFT.value, CampaignStatus.SCHEDULED.value):
        raise ManagedCampaignRefused(f"campaign {campaign.id} is already {campaign.status}")
    moment = at or datetime.now(UTC)
    if machine_names is not None:
        campaign.machine_names = list(machine_names)
    campaign.status = CampaignStatus.ACTIVE.value
    campaign.window_start = moment
    campaign.window_end = moment + start_for
    campaign.override_until = moment + start_for
    session.add(
        Event(
            actor=f"plugin:{plugin}",
            kind="campaign_started",
            campaign_id=campaign.id,
            payload={
                "machines": list(campaign.machine_names or []),
                "window_end": campaign.window_end.isoformat(),
            },
        )
    )


def extend_managed_campaign(
    session: Session, campaign: Campaign, *, plugin: str, until: datetime
) -> None:
    """Move a running managed campaign's window end to `until`, earlier or
    later. For work whose clock should start when it is placed rather than
    when it was started: keep moving the end while nothing has run yet. A
    campaign that is not running is left alone."""
    _check_owner(session, campaign, plugin)
    if campaign.status != CampaignStatus.ACTIVE.value:
        return
    campaign.window_end = until
    campaign.override_until = until


def pin_managed_dataset(
    session: Session,
    campaign: Campaign,
    *,
    plugin: str,
    build: DatasetBuild | None,
    applied: str = "",
    at: datetime | None = None,
) -> None:
    """Give a managed campaign this dataset build, the way the platform pins
    one at a campaign's first measurement, or clear its pin (`build=None`).

    For a plugin that holds several campaigns to one build so they compare:
    a campaign holding a build, started or not, keeps the dataset from being
    rebuilt under it (app/datasets/pinning.py). `applied` is how the build
    was chosen, one of the `pinning.APPLIED_*` values; with no build it says
    why there is none (`APPLIED_UNAVAILABLE`: the campaign then runs without
    waiting for one), and "" leaves the campaign to pin its own."""
    _check_owner(session, campaign, plugin)
    build_id, sha = (build.build_id, build.sha256) if build is not None else ("", "")
    if (
        campaign.dataset_build_id == build_id
        and campaign.dataset_sha256 == sha
        and campaign.dataset_policy_applied == applied
    ):
        return
    if build is None:
        campaign.dataset_build_id = ""
        campaign.dataset_sha256 = ""
        campaign.dataset_pinned_at = (at or datetime.now(UTC)) if applied else None
        campaign.dataset_policy_applied = applied
        campaign.dataset_build_row = 0
        return
    campaign.dataset_build_id = build.build_id
    campaign.dataset_sha256 = build.sha256
    campaign.dataset_pinned_at = at or datetime.now(UTC)
    campaign.dataset_policy_applied = applied
    campaign.dataset_build_row = 0


def stop_managed_campaign(
    session: Session, campaign: Campaign, *, plugin: str, reason: str = ""
) -> bool:
    """Close a managed campaign: no new runs, and its window shut now, so the
    platform ends whatever is still running the way it ends any campaign's
    runs at the end of a window (their results so far stay). A live policy
    session is aborted: it is not asked to finalize, so nothing more is
    measured. Returns True once the campaign is DONE, which is when nothing
    of it is still running; call it again on a later tick until then.
    Idempotent."""
    _check_owner(session, campaign, plugin)
    if campaign.status == CampaignStatus.DONE.value:
        return True
    moment = datetime.now(UTC)
    campaign.override_until = None
    if campaign.window_end is None or as_utc(campaign.window_end) > moment:
        campaign.window_end = moment
    sessions = session.scalars(
        select(PolicySession).where(
            PolicySession.campaign_id == campaign.id,
            PolicySession.status.not_in([s.value for s in TERMINAL_SESSION_STATES]),
        )
    ).all()
    for live_session in sessions:
        if live_session.abort_requested_at is None:
            live_session.abort_requested_at = moment
            session.add(
                Event(
                    actor=f"plugin:{plugin}",
                    kind="policy_session_abort_requested",
                    campaign_id=campaign.id,
                    payload={"session_id": live_session.id, "reason": reason or "stopped"},
                )
            )
    live = session.scalar(
        select(func.count(Run.id)).where(
            Run.campaign_id == campaign.id,
            Run.status.not_in([s.value for s in TERMINAL_RUN_STATES]),
        )
    )
    if live or sessions:
        return False
    campaign.status = CampaignStatus.DONE.value
    session.add(
        Event(
            actor=f"plugin:{plugin}",
            kind="campaign_done",
            campaign_id=campaign.id,
            payload={"reason": reason} if reason else {},
        )
    )
    return True


def owners_of(session: Session, campaign_ids: Iterable[int]) -> dict[int, Owner]:
    """The owner of each of these campaigns that a plugin runs."""
    ids = list(campaign_ids)
    if not ids:
        return {}
    rows = session.scalars(select(CampaignOwner).where(CampaignOwner.campaign_id.in_(ids))).all()
    return {r.campaign_id: Owner(plugin=r.plugin, label=r.label, link=r.link) for r in rows}


def record_event(
    session: Session | AsyncSession,
    kind: str,
    *,
    plugin: str,
    campaign_id: int | None = None,
    run_id: int | None = None,
    payload: dict[str, Any] | None = None,
    actor: str = "",
) -> None:
    """Add to the platform's event log. The log is what a campaign's Activity
    tab and the reports read. `actor` is who did it, for something a person
    asked for (their username); without it the event is the plugin's own
    (`plugin:<name>`). Takes a sync or an async session: it only adds the
    row, which commits with the caller's."""
    session.add(
        Event(
            actor=actor or f"plugin:{plugin}",
            kind=kind,
            campaign_id=campaign_id,
            run_id=run_id,
            payload=payload or {},
        )
    )
