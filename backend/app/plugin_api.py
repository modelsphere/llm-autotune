"""Everything a plugin may import from the platform, and nothing else.

A plugin that imports only from here keeps working across releases with the
same PLUGIN_API_VERSION: a name in this module is changed or removed only
with a new major version, announced under "Plugin API" in the CHANGELOG.
Everything else in `app` is private to the platform and can change in any
release; a plugin that reaches into it is on its own (ruff's `banned-api` can
enforce the boundary in the plugin's repository, docs/plugins.md shows how).

Names are added here when a plugin needs them, not ahead of time: each one
is something the platform can no longer change freely.
"""

from app.control.orchestrator.supervisor import Supervisor
from app.control.search import CandidateConfig
from app.control.search.history import RunRecord
from app.control.search.space import (
    RangeSpec,
    axes,
    expand,
    grid_values,
    prune_inactive,
    range_specs,
    tied_groups,
)
from app.core.auth import get_current_user, require_admin
from app.core.config import get_settings
from app.db.base import get_async_session, sync_session_factory
from app.db.models import (
    Baseline,
    Campaign,
    CampaignStatus,
    Candidate,
    CandidateStatus,
    Event,
    Machine,
    MachineState,
    PolicySession,
    Run,
    RunStatus,
    User,
    UserRole,
)
from app.objective import direction
from app.plugins import (
    PLUGIN_API_VERSION,
    CampaignCreated,
    CampaignExtensions,
    ExtensionRefused,
    PlanContext,
    Plugin,
    Proposer,
    TickStep,
    migration_env,
)

__all__ = [
    "PLUGIN_API_VERSION",
    "Baseline",
    "Campaign",
    "CampaignCreated",
    "CampaignExtensions",
    "CampaignStatus",
    "Candidate",
    "CandidateConfig",
    "CandidateStatus",
    "Event",
    "ExtensionRefused",
    "Machine",
    "MachineState",
    "PlanContext",
    "Plugin",
    "PolicySession",
    "Proposer",
    "RangeSpec",
    "Run",
    "RunRecord",
    "RunStatus",
    "Supervisor",
    "TickStep",
    "User",
    "UserRole",
    "axes",
    "direction",
    "expand",
    "get_async_session",
    "get_current_user",
    "get_settings",
    "grid_values",
    "migration_env",
    "prune_inactive",
    "range_specs",
    "require_admin",
    "sync_session_factory",
    "tied_groups",
]
