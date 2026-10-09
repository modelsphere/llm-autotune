"""What a plugin knows about a run that the platform's own rows do not.

A plugin that starts runs on behalf of something of its own (a request made
in its own pages, say) answers `Plugin.run_overlay(session, run, campaign)`
with a RunOverlay, and the run's agent document is built from it wherever it
says something: its launch configuration, the module verdicts frozen when
the plugin harvested the result, the SLO it holds the run to, the group of
runs it compares with. Every field is optional; what it leaves out is built
from the platform's rows as usual.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.control.launch_config import LaunchConfig
    from app.evaluation.gate import Gate
    from app.schemas.agent import BenchmarkPlatformOut, RunGroup


@dataclass
class RunOverlay:
    # -- launch
    launch: LaunchConfig | None = None  # in place of campaign + candidate
    cards: int | None = None
    launch_command: str = ""  # when the run row has none
    label: str = ""  # how the run is named in a comparison
    source: str = ""  # LaunchOrigin.source, e.g. the plugin's request kind
    origin: dict[str, Any] = field(default_factory=dict)  # LaunchOrigin.extensions
    # -- environment
    env_snapshot: dict[str, Any] = field(default_factory=dict)  # when the run has none
    machine_name: str = ""  # when the run's machine is gone
    # -- benchmark
    module_reports: list[dict[str, Any]] | None = None  # verdicts frozen at harvest
    benchmark: dict[str, Any] = field(default_factory=dict)
    # slug, config_hash_frozen, config_hash_recorded, benchmark_id,
    # submission_id (LLMBench's), dataset_build_id
    platform: BenchmarkPlatformOut | None = None  # in place of the campaign's
    # -- results
    metrics: dict[str, Any] = field(default_factory=dict)  # over the result's
    verdict: dict[str, Any] = field(default_factory=dict)
    # passed, feasible, score, objective_value — used when there is no result
    gate: Gate | None = None  # the SLO levels are judged against
    quality_floors: dict[str, float] | None = None  # in place of the objective's
    ranking_metric: str = ""
    # -- comparison
    group: RunGroup | None = None
