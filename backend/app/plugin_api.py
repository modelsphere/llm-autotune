"""Everything a plugin may import from the platform, and nothing else.

A plugin that imports only from here keeps working across releases with the
same PLUGIN_API_VERSION: a name in this module is changed or removed only
with a new major version, announced under "Plugin API" in the CHANGELOG.
Everything else in `app` is private to the platform and can change in any
release; a plugin that reaches into it is on its own (ruff's `banned-api`
enforces the boundary in the plugin's repository; docs/plugins.md shows how).

Three rules come with it (docs/plugins.md, "What a plugin may touch"):

- The platform's tables are read-only to a plugin. It reads campaigns, runs,
  results and machines freely, and changes them only through the services
  here: a campaign it needs is a managed campaign, and what it wants on the
  record goes through `record_event`.
- A plugin's own tables live on its own metadata and migrations, and point
  at the platform's only by primary key.
- A plugin that builds on another names it in `Plugin.requires`, and may then
  import that plugin's modules.

Names are added here when a plugin needs them, not ahead of time: each one
is something the platform can no longer change freely.
"""

from app.agent.loader import RunBundle, load_run
from app.agent.overlay import RunOverlay
from app.agent.parts import benchmark_part, launch_part, results_part
from app.campaign_rules import campaign_errors
from app.control.engine_command import baseline_engine_args, parse_launch_command
from app.control.engines.base import MODEL_MOUNT
from app.control.engines.flags import (
    catalog_payload,
    known_flags,
    negated_flag,
    normalize_args,
    render_flag,
)
from app.control.launch import MachineInfo, get_driver, preflight
from app.control.launch_config import LaunchConfig
from app.control.managed import (
    ManagedCampaignRefused,
    Owner,
    create_managed_campaign,
    extend_managed_campaign,
    owners_of,
    pin_managed_dataset,
    record_event,
    start_managed_campaign,
    stop_managed_campaign,
)
from app.control.orchestrator.groups import group_blockers, group_lease_warnings, members_by_rank
from app.control.orchestrator.lifecycle import (
    accepts_new_work,
    as_utc,
    lease_expired,
    leased_target_count,
    machine_has_live_session,
    machine_warnings,
    now,
)
from app.control.orchestrator.machine_queue import campaign_key, session_key
from app.control.orchestrator.occupancy import Reservation, busy_reason, reservations_on
from app.control.orchestrator.policy_lifecycle import settings_of as policy_settings_of
from app.control.orchestrator.supervisor import Supervisor
from app.control.recipe.check import recipe_problems
from app.control.recipe.command import command_flags as recipe_command_flags
from app.control.recipe.command import recipe_flags
from app.control.recipe.importer import NotARecipe
from app.control.recipe.importer import parse as parse_recipe
from app.control.recipe.model import Recipe
from app.control.recipe.shape import pod_gpus as recipe_pod_gpus
from app.control.recipe.shape import shape as recipe_shape
from app.control.run_nodes import run_ids_on_machine
from app.control.search import CandidateConfig
from app.control.search.space import (
    RangeSpec,
    axes,
    candidate_count,
    expand,
    grid_values,
    prune_inactive,
    range_specs,
    space_errors,
    tied_groups,
)
from app.control.search.validation import cards_used, widest_cards
from app.core.auth import (
    Principal,
    get_current_user,
    get_principal,
    require_admin,
    require_admin_principal,
)
from app.core.config import get_settings
from app.core.platform_settings import LlmTarget, reports_llm
from app.core.secrets import decrypt_secret, encrypt_secret
from app.datasets import pinning
from app.datasets.profiles import BuildInFlight, DatasetBuild, DatasetProfileError
from app.db.base import get_async_session, sync_session_factory
from app.db.models import (
    TERMINAL_RUN_STATES,
    TERMINAL_SESSION_STATES,
    AgentReport,
    Baseline,
    Campaign,
    CampaignStatus,
    Candidate,
    CandidateKind,
    CandidateStatus,
    Cluster,
    Event,
    Machine,
    MachineGroup,
    MachineState,
    Policy,
    PolicySession,
    PolicySessionStatus,
    Result,
    Run,
    RunKind,
    RunStatus,
    User,
    UserRole,
)
from app.db.models import JsonCol as JsonType
from app.evaluation import scenarios
from app.evaluation.aggregate import ResultSummary, summary_of
from app.evaluation.gate import Gate
from app.evaluation.llmbench import LLMBenchClient, instance_keys, module_reports
from app.hardware import coerce_gpu_type, normalize_gpu_type
from app.metrics_catalog import DEFAULT_TARGET_METRIC, DEFAULT_VERIFY_TARGET_METRIC
from app.objective import direction, improvement_pct, sort_key, target_metric
from app.plugins import (
    PLUGIN_API_VERSION,
    CampaignCreated,
    CampaignExtensions,
    ExtensionRefused,
    Plugin,
    QueueWaiter,
    SelectorRefused,
    TickStep,
    migration_env,
)
from app.schemas.agent import (
    SCHEMA_VERSION,
    BenchmarkLLMBenchOut,
    BenchmarkPart,
    BenchmarkPlatformOut,
    ResultsHeadline,
    RunGroup,
)
from app.schemas.core import CampaignCreate, MachineWarningOut, UserOut
from app.staging import SCREEN, VERIFY

__all__ = [
    # Hooks and the plugin itself
    "PLUGIN_API_VERSION",
    "CampaignCreated",
    "CampaignExtensions",
    "ExtensionRefused",
    "Plugin",
    "QueueWaiter",
    "Reservation",
    "RunOverlay",
    "SelectorRefused",
    "Supervisor",
    "TickStep",
    "migration_env",
    # Managed campaigns: how a plugin runs work
    "CampaignCreate",
    "ManagedCampaignRefused",
    "Owner",
    "campaign_errors",
    "create_managed_campaign",
    "extend_managed_campaign",
    "owners_of",
    "pin_managed_dataset",
    "record_event",
    "start_managed_campaign",
    "stop_managed_campaign",
    # The platform's tables (read-only to plugins)
    "TERMINAL_RUN_STATES",
    "TERMINAL_SESSION_STATES",
    "AgentReport",
    "Baseline",
    "Campaign",
    "Cluster",
    "CampaignStatus",
    "Candidate",
    "CandidateKind",
    "CandidateStatus",
    "Event",
    "JsonType",
    "Machine",
    "MachineGroup",
    "MachineState",
    "Policy",
    "PolicySession",
    "PolicySessionStatus",
    "Result",
    "Run",
    "RunKind",
    "RunStatus",
    "User",
    "UserRole",
    # Users, sessions, settings, secrets
    "Principal",
    "UserOut",
    "decrypt_secret",
    "encrypt_secret",
    "get_async_session",
    "get_current_user",
    "get_principal",
    "get_settings",
    # The LLM endpoint an admin chose for report writing (Settings), or None.
    "LlmTarget",
    "reports_llm",
    "require_admin",
    "require_admin_principal",
    "sync_session_factory",
    # Machines, the queue and the clock
    "MachineInfo",
    "MachineWarningOut",
    "accepts_new_work",
    "as_utc",
    "busy_reason",
    "campaign_key",
    "get_driver",
    "group_blockers",
    "group_lease_warnings",
    "lease_expired",
    "leased_target_count",
    "machine_has_live_session",
    "machine_warnings",
    "members_by_rank",
    "now",
    "policy_settings_of",
    "preflight",
    "reservations_on",
    "run_ids_on_machine",
    "session_key",
    # The search space
    "CandidateConfig",
    "RangeSpec",
    "axes",
    "candidate_count",
    "cards_used",
    "expand",
    "grid_values",
    "prune_inactive",
    "range_specs",
    "space_errors",
    "tied_groups",
    "widest_cards",
    # Recipes: a configuration as the chart and the catalog read it
    "NotARecipe",
    "Recipe",
    "parse_recipe",
    "recipe_command_flags",
    "recipe_flags",
    "recipe_pod_gpus",
    "recipe_problems",
    "recipe_shape",
    # Engines and configs
    "MODEL_MOUNT",
    "LaunchConfig",
    "baseline_engine_args",
    "catalog_payload",
    "coerce_gpu_type",
    "known_flags",
    "negated_flag",
    "normalize_args",
    "normalize_gpu_type",
    "parse_launch_command",
    "render_flag",
    # Measuring: objectives, results, LLMBench, datasets
    "DEFAULT_TARGET_METRIC",
    "DEFAULT_VERIFY_TARGET_METRIC",
    "SCREEN",
    "VERIFY",
    "BuildInFlight",
    "DatasetBuild",
    "DatasetProfileError",
    "Gate",
    "LLMBenchClient",
    "ResultSummary",
    "direction",
    "improvement_pct",
    "instance_keys",
    "module_reports",
    "pinning",
    "scenarios",
    "sort_key",
    "summary_of",
    "target_metric",
    # The agent API's run documents
    "SCHEMA_VERSION",
    "BenchmarkLLMBenchOut",
    "BenchmarkPlatformOut",
    "RunGroup",
    "BenchmarkPart",
    "ResultsHeadline",
    "RunBundle",
    "benchmark_part",
    "launch_part",
    "load_run",
    "results_part",
]
