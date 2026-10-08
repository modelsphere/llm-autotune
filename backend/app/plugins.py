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
from functools import lru_cache
from importlib.metadata import EntryPoint, entry_points
from typing import TYPE_CHECKING, Any

from app.core.config import get_settings

if TYPE_CHECKING:
    from fastapi import APIRouter
    from sqlalchemy import Engine, MetaData
    from sqlalchemy.orm import Session

    from app.control.orchestrator.supervisor import Supervisor
    from app.control.search import CandidateConfig
    from app.control.search.history import RunRecord
    from app.db.models import Campaign

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


@dataclass(frozen=True)
class Plugin:
    """What a plugin adds. Every hook is optional.

    name: lowercase letters, digits and underscores; the name AUTOTUNE_PLUGINS
        lists, and the suffix of the plugin's migration version table.
    api_version: the PLUGIN_API_VERSION the plugin was written for.
    routers: FastAPI routers, mounted under /api after the platform's own.
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
    """

    name: str
    api_version: int
    routers: Sequence[APIRouter] = ()
    tick_steps: Sequence[TickStep] = ()
    migrations: str | None = None
    on_bootstrap: Callable[[Engine], None] | None = None
    propose_candidates: Proposer | None = None

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
