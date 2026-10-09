"""Plugins: packages that add to the platform without editing it.

A plugin is a Python package installed into the platform's environment that
declares a `Plugin` under the entry-point group `llm_autotune.plugins`:

    [project.entry-points."llm_autotune.plugins"]
    example = "example_plugin:plugin"

Installed is not enabled. Only the names listed in AUTOTUNE_PLUGINS load, so
an image can carry a plugin that a deployment leaves off. A listed plugin
that is not installed, or was written for another version of this API, stops
the process at startup with a message saying which: a platform running with
half its features quietly missing is worse than one that does not start.

What a plugin may import from the platform is `app.plugin_api`; anything else
in `app` can change in any release. Each hook is a field of `Plugin`, and all
of them are optional (docs/plugins.md describes them).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from importlib.metadata import EntryPoint, entry_points
from typing import TYPE_CHECKING, Any

from app.core.config import get_settings

if TYPE_CHECKING:
    from fastapi import APIRouter
    from sqlalchemy import Engine, MetaData
    from sqlalchemy.orm import Session

    from app.agent.overlay import RunOverlay
    from app.control.orchestrator.occupancy import Reservation
    from app.control.orchestrator.supervisor import Supervisor
    from app.control.search import CandidateConfig
    from app.control.search.history import RunRecord
    from app.db.models import Campaign, Machine, Run

logger = logging.getLogger(__name__)

# The version of the hooks and of `app.plugin_api`. It changes only when a
# change would break a plugin written against the previous one; additions do
# not change it. A plugin states the version it was written for.
PLUGIN_API_VERSION = 1

ENTRY_POINT_GROUP = "llm_autotune.plugins"

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

TickStep = Callable[["Supervisor", "Session"], None]


class PluginError(RuntimeError):
    """A plugin that cannot be loaded as configured."""


@dataclass(frozen=True)
class PlanContext:
    """What a planner is handed about one ACTIVE campaign, every tick.

    `session` is the worker's own; read from it, and leave the candidate
    rows to the platform: whatever `propose_candidates` returns is validated,
    deduplicated against what the campaign already holds, and saved."""

    session: Session
    campaign: Campaign
    supervisor: Supervisor

    # Unstarted work worth queuing at once for a space whose points do not
    # depend on results (a grid): the queue refills as runs consume it.
    batch_cap: int = 100

    def history(self) -> list[RunRecord]:
        """Every run and queued or rejected candidate, objective applied."""
        from app.control.search.history import campaign_history

        return campaign_history(self.session, self.campaign)

    def queued(self) -> int:
        """Valid candidates waiting for a run."""
        from sqlalchemy import func, select

        from app.db.models import Candidate, CandidateStatus

        return (
            self.session.scalar(
                select(func.count(Candidate.id)).where(
                    Candidate.campaign_id == self.campaign.id,
                    Candidate.status == CandidateStatus.VALID.value,
                )
            )
            or 0
        )

    def startable_slots(self) -> int:
        """How many runs this campaign could start right now on its machines.
        An adaptive planner asks for about this many points: asking a model
        for a hundred with eight cards free commits to a hundred guesses
        before any result can teach it anything."""
        return self.supervisor._startable_slots(self.session, self.campaign)


Proposer = Callable[[PlanContext], "Sequence[CandidateConfig] | None"]
CampaignCreated = Callable[["Session", "Campaign", dict[str, Any]], None]
CampaignExtensions = Callable[["Session", "Sequence[Campaign]"], dict[int, dict[str, Any]]]


@dataclass(frozen=True)
class QueueWaiter:
    """Something of a plugin's waiting for a machine in the machine queue.

    It takes its turn in arrival order among the platform's own waiters
    (campaigns with a run ready, policy sessions without a machine). On its
    turn the platform calls `try_admit(session, blocked, busy)`: `blocked`
    are machines an older waiter holds; return True once it took what it
    needed (typically admitting a campaign to a machine, which then holds it
    through `Plugin.reservations` until its run is placed), or False after
    adding to `busy` the machines it could use once they free up, so nobody
    behind it takes them. It runs in a savepoint; one that raises is logged
    and counts as not admitted."""

    arrival: datetime
    try_admit: Callable[[Session, set[int], set[int]], bool]
    label: str
    # Breaks ties at the same instant, lowest first: a plugin's waiter 0 by
    # default, a campaign 1, a policy session 2.
    rank: int = 0
    ident: int = 0


@dataclass(frozen=True)
class PromotionOrigin:
    """What a campaign's winner stands for, when a plugin runs the campaign
    for something of its own: what the merge request calls it, where it
    links back to, and the release branch it goes onto when the campaign
    names none."""

    kind: str
    page: str = ""
    description: str = ""
    deploy_branch: str = ""
    # Whether a branch named on a promotion sticks to the campaign.
    remember_branch: bool = True


class SelectorRefused(ValueError):
    """Raised by a run_selector to answer the agent API with this error:
    `code` (machine-readable), `status` (HTTP), `detail`."""

    def __init__(self, code: str, status: int = 404, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code, self.status, self.detail = code, status, detail


class ExtensionRefused(ValueError):
    """Raised by `on_campaign_created` to refuse what it was given; the
    campaign is not created, and the message is the API's 422 answer."""


@dataclass(frozen=True)
class Plugin:
    """What a plugin adds. Every hook is optional.

    name: lowercase letters, digits and underscores; the name AUTOTUNE_PLUGINS
        lists, and the suffix of the plugin's migration version table.
    api_version: the PLUGIN_API_VERSION the plugin was written for.
    routers: FastAPI routers, mounted under /api after the platform's own.
    openapi_tags: descriptions for the tags those routers use, as FastAPI's
        `openapi_tags` entries, shown in the API docs with the platform's.
    tick_steps: functions the worker calls at the START of every tick, each
        as `step(supervisor, session)`, inside a savepoint: a step that raises
        is logged and rolled back, and the rest of the tick goes on. Running
        first means whatever a step starts (an ACTIVE campaign, say) is
        planned, scheduled and advanced by the rest of the same tick.
    migrations: the path of the plugin's own Alembic directory. It is upgraded
        to head after the platform's schema on every deploy, and keeps its
        place in its own version table (see `migration_env`).
    on_bootstrap: called after the platform's own seeding on every deploy,
        with a sync engine. Like the platform's seeding it should only ever
        add what is missing.
    propose_candidates: plans a campaign in-process instead of the default
        enumeration of its space. Called with a `PlanContext` for every ACTIVE
        campaign that has no policy container, each tick; returns the points
        to add now (possibly none), or None for a campaign this plugin does
        not plan. The first plugin to answer anything but None plans it. One
        that raises is logged, and the campaign gets no new candidates that
        tick.
    on_campaign_created: `(session, campaign, data)`, when a campaign is
        created (from the form, an imported spec or a clone) with
        `extensions[<plugin name>] = data`. It runs in the same transaction:
        raising `ExtensionRefused` refuses the campaign with that message.
        This is how a plugin keeps its own fields about a campaign, in its
        own side table.
    campaign_extensions: `(session, campaigns) -> {campaign_id: data}`, what
        the plugin keeps about these campaigns; the API shows it as
        `extensions[<plugin name>]`, and a spec or clone carries it back into
        `on_campaign_created`. One that raises is logged and left out: a
        plugin must not stop campaigns from being read.
    queue_waiters: `(supervisor, session) -> [QueueWaiter]`, the plugin's
        waiters in this tick's machine queue (see QueueWaiter).
    reservations: `(session, machine) -> [Reservation]`, the machines the
        plugin holds for campaigns whose run is not placed yet. Placement,
        policy sessions and every waiter read them, so a held machine is held
        for everyone.
    submission_extras: `(session, run, context) -> dict`, fields for the
        run's LLMBench submission, given what the platform will send
        (`context`: benchmark_slug, hardware, contributor, source_url…).
        `contributor` and `source_url` replace the platform's; anything else
        is added to the submission body as is. One that raises is logged and
        the submission goes out without it.
    run_overlay: `(session, run, campaign) -> RunOverlay | None`, what the
        plugin knows about a run it started on behalf of something of its own,
        for the run's agent documents (app/agent/overlay.py). The first plugin
        to answer anything but None describes it; one that raises is logged
        and left out.
    run_selector: `(session, token) -> run id | None`, for a run selector
        the agent API does not know (`?baseline=` and friends take run ids;
        a plugin may accept its own spelling, e.g. a request id). Raise
        SelectorRefused to answer with a specific error.
    promotion_origin: `(session, campaign, run_id) -> PromotionOrigin | None`,
        what a winner of this campaign stands for in its merge request — its
        name, its page, a fallback release branch — for a campaign the plugin
        runs for something of its own. The first answer wins.
    queue_arrival: `(session, campaign) -> datetime | None`, when a campaign
        joined the machine queue, for a campaign that represents something
        older than itself (a request made before its campaign existed). None
        leaves the platform's own answer.
    """

    name: str
    api_version: int
    routers: Sequence[APIRouter] = ()
    openapi_tags: Sequence[dict[str, Any]] = ()
    tick_steps: Sequence[TickStep] = ()
    migrations: str | None = None
    on_bootstrap: Callable[[Engine], None] | None = None
    propose_candidates: Proposer | None = None
    on_campaign_created: CampaignCreated | None = None
    campaign_extensions: CampaignExtensions | None = None
    queue_waiters: Callable[[Supervisor, Session], Iterable[QueueWaiter]] | None = None
    reservations: Callable[[Session, Machine], Iterable[Reservation]] | None = None
    queue_arrival: Callable[[Session, Campaign], datetime | None] | None = None
    submission_extras: Callable[[Session, Run, dict[str, Any]], dict[str, Any]] | None = None
    run_overlay: Callable[[Session, Run, Campaign | None], RunOverlay | None] | None = None
    run_selector: Callable[[Session, str], int | None] | None = None
    promotion_origin: Callable[[Session, Campaign, int], PromotionOrigin | None] | None = None

    @property
    def version_table(self) -> str:
        return f"alembic_version_{self.name}"


def _installed() -> dict[str, EntryPoint]:
    return {ep.name: ep for ep in entry_points(group=ENTRY_POINT_GROUP)}


def load(names: Iterable[str], installed: dict[str, EntryPoint] | None = None) -> list[Plugin]:
    """The plugins with these entry-point names, in the order given."""
    available = _installed() if installed is None else installed
    plugins: list[Plugin] = []
    for name in names:
        if name in (p.name for p in plugins):
            raise PluginError(f"plugin {name!r} is listed twice in AUTOTUNE_PLUGINS")
        entry = available.get(name)
        if entry is None:
            have = ", ".join(sorted(available)) or "none"
            raise PluginError(
                f"AUTOTUNE_PLUGINS lists {name!r}, but no installed package provides "
                f"it (installed plugins: {have})"
            )
        try:
            obj: Any = entry.load()
        except Exception as exc:
            raise PluginError(f"plugin {name!r} failed to import: {exc}") from exc
        if not isinstance(obj, Plugin):
            raise PluginError(
                f"plugin {name!r} points at {entry.value}, which is not an app.plugins.Plugin"
            )
        if obj.name != name:
            raise PluginError(
                f"plugin {name!r} names itself {obj.name!r}; the entry-point name and "
                "Plugin.name must match"
            )
        if not _NAME.match(obj.name):
            raise PluginError(
                f"plugin name {obj.name!r} must be lowercase letters, digits and underscores"
            )
        if obj.api_version != PLUGIN_API_VERSION:
            raise PluginError(
                f"plugin {name!r} was written for plugin API {obj.api_version}, and this "
                f"platform provides {PLUGIN_API_VERSION}: use a version of the plugin "
                "made for this release"
            )
        plugins.append(obj)
    return plugins


def configured_names() -> list[str]:
    return [n.strip() for n in get_settings().plugins.split(",") if n.strip()]


@lru_cache
def enabled() -> tuple[Plugin, ...]:
    """The plugins AUTOTUNE_PLUGINS enables, loaded once per process."""
    plugins = tuple(load(configured_names()))
    for plugin in plugins:
        logger.info("plugin %s enabled", plugin.name)
    return plugins


def create_campaign_extensions(
    session: Session, campaign: Campaign, extensions: dict[str, dict[str, Any]]
) -> None:
    """Hand each plugin its part of a new campaign's `extensions`. A key no
    enabled plugin takes is refused rather than dropped: it is a campaign
    that would quietly run without what its author asked for."""
    takers = {p.name: p for p in enabled() if p.on_campaign_created is not None}
    unknown = sorted(set(extensions) - set(takers))
    if unknown:
        raise ExtensionRefused(
            f"extensions for {', '.join(unknown)}: no enabled plugin by that name "
            "keeps campaign extensions"
        )
    for name, data in extensions.items():
        takers[name].on_campaign_created(session, campaign, data or {})


def read_campaign_extensions(
    session: Session, campaigns: Sequence[Campaign]
) -> dict[int, dict[str, dict[str, Any]]]:
    """`{campaign_id: {plugin name: data}}`, from every plugin that keeps any."""
    out: dict[int, dict[str, dict[str, Any]]] = {c.id: {} for c in campaigns}
    if not campaigns:
        return out
    for plugin in enabled():
        if plugin.campaign_extensions is None:
            continue
        try:
            # A savepoint, so a failed query leaves the request's transaction
            # usable (Postgres aborts the whole transaction otherwise).
            with session.begin_nested():
                found = plugin.campaign_extensions(session, campaigns)
        except Exception:
            logger.exception("plugin %s: reading campaign extensions failed", plugin.name)
            continue
        for campaign_id, data in (found or {}).items():
            if data and campaign_id in out:
                out[campaign_id][plugin.name] = data
    return out


def run_overlay(session: Session, run: Any, campaign: Any) -> RunOverlay | None:
    """What the first plugin that knows this run says about it, or None."""
    for plugin in enabled():
        if plugin.run_overlay is None:
            continue
        try:
            overlay = plugin.run_overlay(session, run, campaign)
        except Exception:
            logger.exception("plugin %s: describing run %s failed", plugin.name, run.id)
            continue
        if overlay is not None:
            return overlay
    return None


def promotion_origin_of(session: Session, campaign: Any, run_id: int) -> PromotionOrigin | None:
    """What the first plugin that knows this campaign says its winner is."""
    for plugin in enabled():
        if plugin.promotion_origin is None:
            continue
        try:
            told = plugin.promotion_origin(session, campaign, run_id)
        except Exception:
            logger.exception("plugin %s: promotion origin failed", plugin.name)
            continue
        if told is not None:
            return told
    return None


def resolve_run_selector(session: Session, token: str) -> int | None:
    """A run id for a selector only a plugin knows, or None. SelectorRefused
    propagates: the plugin knew the selector and refused it."""
    for plugin in enabled():
        if plugin.run_selector is None:
            continue
        run_id = plugin.run_selector(session, token)
        if run_id is not None:
            return run_id
    return None


def mount(app: Any, mounted: Iterable[Plugin], prefix: str) -> None:
    """Each plugin's routers under `prefix`, and its tag descriptions in the
    API docs next to the platform's."""
    for plugin in mounted:
        for router in plugin.routers:
            app.include_router(router, prefix=prefix)
        app.openapi_tags = [*(app.openapi_tags or []), *plugin.openapi_tags]


def migration_env(metadata: MetaData, plugin_name: str) -> None:
    """The body of a plugin's alembic `env.py`:

        from app.plugin_api import migration_env
        from my_plugin.models import Base
        migration_env(Base.metadata, "my_plugin")

    The plugin's tables live on its own metadata, never on the platform's, and
    its revisions are recorded in `alembic_version_<name>`, so the platform's
    and each plugin's histories advance independently. Comparisons
    (`alembic check`) see only the plugin's own tables."""
    from sqlalchemy import engine_from_config, pool

    from alembic import context

    config = context.config
    url = config.get_main_option("sqlalchemy.url") or get_settings().sync_database_url
    config.set_main_option("sqlalchemy.url", url)
    options: dict[str, Any] = {
        "target_metadata": metadata,
        "version_table": f"alembic_version_{plugin_name}",
        "include_object": only_tables_of(metadata),
    }
    if context.is_offline_mode():
        context.configure(url=url, literal_binds=True, **options)
        with context.begin_transaction():
            context.run_migrations()
        return
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, **options)
        with context.begin_transaction():
            context.run_migrations()


def only_tables_of(metadata: MetaData) -> Callable[..., bool]:
    """An alembic `include_object` that compares only the tables `metadata`
    defines: the database holds the platform's tables and every plugin's, and
    each migration history answers for its own."""
    own = set(metadata.tables)

    def include(obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any) -> bool:
        table = obj if type_ == "table" else getattr(obj, "table", None)
        table_name = name if type_ == "table" else getattr(table, "name", None)
        if table_name is None:
            return True
        return table_name in own

    return include


def upgrade(plugin: Plugin, database_url: str) -> None:
    """Bring a plugin's own schema to its head."""
    if not plugin.migrations:
        return
    from alembic.config import Config

    from alembic import command

    config = Config()
    config.set_main_option("script_location", plugin.migrations)
    config.set_main_option("sqlalchemy.url", database_url)
    logger.info("plugin %s: upgrading its schema to head", plugin.name)
    command.upgrade(config, "head")
