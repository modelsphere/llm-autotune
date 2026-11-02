"""Measures: the platform's one vocabulary for benchmark results.

    scenarios = measures_of(result.metrics, reports, cards=2)
    value("ttft_ms@p99", scenarios)            # the main scenario's operating point
    value("replay/success_rate", scenarios)
    value("replay.output_tpm_card_norm", metrics=result.metrics)   # a raw key, verbatim

See `catalog` for the measures, `refs` for how one is named and `adapters`
for how each LLMBench module's keys become them.
"""

from __future__ import annotations

from typing import Any

from app.measures.adapters import Scenario, adapt
from app.measures.catalog import BY_NAME, MEASURES, Measure, lookup
from app.measures.refs import MeasureRef, RefError, is_measure_ref, label, parse

__all__ = [
    "BY_NAME",
    "MEASURES",
    "Measure",
    "MeasureRef",
    "RefError",
    "Scenario",
    "adapt",
    "is_measure_ref",
    "label",
    "lookup",
    "main_scenario",
    "measures_of",
    "parse",
    "resolve",
    "scenarios_out",
    "value",
]

# Keys at the top of a run's metrics that belong to no module.
_SUBMISSION_KEYS = {"score_total"}


def _split(metrics: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """A run's flat `<module>.<key>` metrics, grouped by module, in the order
    the modules first appear."""
    out: dict[str, dict[str, Any]] = {}
    for key, value in (metrics or {}).items():
        if key in _SUBMISSION_KEYS or "." not in key:
            continue
        module, _, rest = key.partition(".")
        out.setdefault(module, {})[rest] = value
    return out


def measures_of(
    metrics: dict[str, Any] | None,
    reports: list[dict[str, Any]] | None = None,
    cards: float | None = None,
    replay_names: tuple[str, ...] | None = None,
    aliases: dict[str, str] | None = None,
) -> list[Scenario]:
    """Every module of a run as a Scenario.

    `metrics` is the flat dict a run's Result stores. `reports` (optional) are
    the module reports, for each module's params — a replay's concurrency, a
    sweep's configured token lengths. `cards` is what the config occupied;
    without it per-GPU measures are recovered from LLMBench's card-normalized
    pairs, or left out.
    """
    params_by_key: dict[str, dict[str, Any]] = {}
    for report in reports or []:
        if isinstance(report, dict):
            key = str(report.get("module") or report.get("module_name") or "")
            params_by_key[key] = dict(report.get("params") or {})
    if replay_names is None or aliases is None:
        from app.core.config import get_settings

        settings = get_settings()
        if replay_names is None:
            configured = settings.llmbench_replay_module
            replay_names = (configured,) if configured else ()
        if aliases is None:
            aliases = dict(settings.llmbench_module_families or {})
    return [
        adapt(key, raw, params_by_key.get(key), cards, replay_names, aliases)
        for key, raw in _split(metrics or {}).items()
    ]


def main_scenario(scenarios: list[Scenario], wanted: str | None = None) -> Scenario | None:
    """The scenario a reference without one means: the named one, else the
    first throughput scenario in benchmark order, else the first of any kind."""
    if wanted:
        return next((s for s in scenarios if s.key == wanted), None)
    return next((s for s in scenarios if s.is_throughput), scenarios[0] if scenarios else None)


def resolve(ref: MeasureRef, scenarios: list[Scenario]) -> float | None:
    scenario = main_scenario(scenarios, ref.scenario)
    if scenario is None:
        return None
    point = scenario.points.get(ref.point or "operating") or {}
    return point.get(ref.key)


def value(
    name: str,
    scenarios: list[Scenario] | None = None,
    *,
    metrics: dict[str, Any] | None = None,
) -> Any:
    """What `name` reads to in a run: a measure reference through the
    scenarios, a raw LLMBench key verbatim from the stored metrics."""
    if is_measure_ref(name):
        if scenarios is None:
            scenarios = measures_of(metrics or {})
        return resolve(parse(name), scenarios)
    return (metrics or {}).get(name)


class Reader:
    """One run's numbers by name: a measure reference through its scenarios,
    a raw LLMBench key verbatim. Has the `.get` of the metrics dict it
    replaces, so everything that judges a run (objective, redlines, gate)
    reads both kinds of name without knowing there are two.
    """

    def __init__(
        self,
        metrics: dict[str, Any] | None,
        reports: list[dict[str, Any]] | None = None,
        cards: float | None = None,
    ):
        self.metrics = metrics or {}
        self._reports = reports
        self._cards = cards
        self._scenarios: list[Scenario] | None = None

    @property
    def scenarios(self) -> list[Scenario]:
        if self._scenarios is None:
            self._scenarios = measures_of(self.metrics, self._reports, self._cards)
        return self._scenarios

    def get(self, name: str, default: Any = None) -> Any:
        if name and is_measure_ref(name):
            try:
                found = resolve(parse(name), self.scenarios)
            except RefError:
                return default
            return default if found is None else found
        return self.metrics.get(name, default)

    def __contains__(self, name: str) -> bool:
        return self.get(name) is not None


def reader_for(
    metrics: dict[str, Any] | None,
    raw: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    campaign: Any = None,
) -> Reader:
    """A Reader for a stored result: module params from the raw submission,
    cards from the config it measured (tp × dp × pp).

    With the campaign, the cards are those one launch of the config claims:
    a recipe campaign's config holds only what differs from its recipe, so
    the config alone has no tp and reads as one card (an 8-card launch then
    reported 8× its per-GPU rate), and production's config is empty."""
    reports = None
    if raw:
        from app.evaluation.llmbench import module_reports

        try:
            reports = module_reports(raw)
        except Exception:
            reports = None
    cards = None
    if campaign is not None and getattr(campaign, "recipe", None):
        from app.control.recipe.campaign import config_cards

        try:
            cards = config_cards(campaign, config) or None
        except Exception:
            cards = None
    elif config:
        from app.control.search.validation import cards_used

        try:
            cards = cards_used(config) or None
        except Exception:
            cards = None
    return Reader(metrics, reports, cards)


def scenarios_out(scenarios: list[Scenario]) -> list[dict[str, Any]]:
    """Scenarios as the APIs send them (policy benchmarks, report results):
    the operating point, the other points, what each module can report."""
    return [
        {
            "scenario": sc.key,
            "module": sc.module,
            "kind": sc.kind,
            "operating_level": sc.operating_level or None,
            "operating_rule": sc.operating_rule,
            "operating": dict(sc.operating),
            "points": {k: dict(v) for k, v in sc.points.items() if k != "operating"},
            "supports": sorted(sc.supports),
        }
        for sc in scenarios
    ]


def problem_with(
    name: str,
    modules: list[str],
    replay_names: tuple[str, ...] | None = None,
    aliases: dict[str, str] | None = None,
) -> str | None:
    """Why a benchmark running `modules` (in order) can never report `name`,
    or None when it can. A raw key is the caller's business."""
    from app.evaluation.llmbench import instance_keys
    from app.measures.adapters import adapt

    if not is_measure_ref(name):
        return None
    try:
        ref = parse(name)
    except RefError as exc:
        return str(exc)
    if replay_names is None or aliases is None:
        from app.core.config import get_settings

        settings = get_settings()
        replay_names = (settings.llmbench_replay_module,) if replay_names is None else replay_names
        aliases = dict(settings.llmbench_module_families or {}) if aliases is None else aliases
    scenarios = [
        adapt(key, {}, None, None, replay_names, aliases) for key in instance_keys(modules)
    ]
    scenario = main_scenario(scenarios, ref.scenario)
    if scenario is None:
        return (
            f"no module {ref.scenario} in this benchmark"
            if ref.scenario
            else "this benchmark runs no modules"
        )
    if ref.point and scenario.kind != "sweep":
        return f"{scenario.key} is not a sweep, so it has no point {ref.point}"
    if ref.key not in scenario.supports:
        return f"{scenario.key} never reports {ref.key}"
    return None
