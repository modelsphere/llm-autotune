"""A campaign as a specification: what defines one, and drafting one from what
the platform already knows.

Three ways to start a campaign share this module, so they cannot drift apart:

- **Export / import** — the YAML on a campaign's page, and the file posted back
  to `POST /api/campaigns`. `SPEC_KEYS` is the list of fields that travel.
- **Clone** — `POST /api/campaigns/{id}/clone` is the spec of an existing
  campaign, with overrides, posted through the same create path.
- **Draft** — `POST /api/campaigns/draft` builds a spec from facts the
  platform holds instead of from a form: the model's baseline (image, path,
  production engine arguments), the fleet (card type and count), LLMBench (which
  dataset a benchmark replays), and the metrics catalog (the default goal). It
  never saves anything; the caller reviews it, preflights it, and posts it.

Every drafted field says where it came from (`provenance`), so a reviewer can
tell a derived value from a default and knows which ones to look at.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import catalog
from app.control.baseline import resolve_baseline
from app.control.launch_config import LaunchConfig
from app.control.search.space import candidate_count
from app.core.config import get_settings
from app.db.models import (
    Baseline,
    Campaign,
    LeaseState,
    Machine,
    MachineGroup,
    MachineGroupMember,
)
from app.metrics_catalog import DEFAULT_TARGET_METRIC, DEFAULT_VERIFY_TARGET_METRIC
from app.schemas.core import CampaignCreate

# Fields that are about one run of a campaign, not its definition: they are set
# by the clock and by Force start, and copying them would start a clone at a
# moment that has already passed.
_RUNTIME_ONLY = frozenset({"window_start", "window_end"})

# Every field a campaign is defined by — exactly CampaignCreate's, minus the
# runtime ones. Derived, not listed, so a field added to CampaignCreate travels
# through export, import and clone without anyone remembering to add it here.
# (The frontend keeps its own copy for the YAML editor; a test checks the two.)
SPEC_KEYS: tuple[str, ...] = tuple(
    k for k in CampaignCreate.model_fields if k not in _RUNTIME_ONLY
)

# The drafted grid stays small enough to finish in one night on one machine:
# each point is a launch plus a benchmark, roughly half an hour.
MAX_DRAFT_CANDIDATES = 24


def spec_of(campaign: Campaign) -> dict[str, Any]:
    """The campaign as the body that would create it again."""
    spec = {k: getattr(campaign, k) for k in SPEC_KEYS}
    if spec.get("schedule_until") is not None:
        spec["schedule_until"] = spec["schedule_until"].isoformat()
    return spec


# ---------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------


@dataclass
class DraftRequest:
    baseline_id: int | None = None
    served_model_name: str = ""
    engine: str = ""
    machine_names: list[str] = field(default_factory=list)
    node_group: str = ""
    benchmark_slug: str = ""
    verify_benchmark_slug: str = ""
    policy_id: int | None = None
    daily_start: str = ""
    daily_end: str = ""
    schedule_timezone: str = ""
    name: str = ""


@dataclass
class Draft:
    campaign: dict[str, Any]
    provenance: dict[str, str]
    warnings: list[str]


def _tp_param(engine: str) -> str:
    return "tp" if catalog.lookup(engine, "tp") else "tensor_parallel_size"


def _fleet(session: Session, req: DraftRequest) -> tuple[list[Machine], str]:
    """The machines this campaign would run on, and how that was decided."""
    if req.node_group:
        group = session.scalars(
            select(MachineGroup).where(MachineGroup.name == req.node_group)
        ).first()
        if group is not None:
            machines = session.scalars(
                select(Machine)
                .join(MachineGroupMember, MachineGroupMember.machine_id == Machine.id)
                .where(MachineGroupMember.group_id == group.id)
            ).all()
            if machines:
                return list(machines), f"node group {req.node_group}"
    if req.machine_names:
        machines = session.scalars(select(Machine).where(Machine.name.in_(req.machine_names))).all()
        return list(machines), "the machines you named"
    leased = session.scalars(
        select(Machine).where(Machine.lease_state == LeaseState.ACTIVE.value)
    ).all()
    return list(leased), "every machine currently leased"


def _card_facts(machines: list[Machine]) -> tuple[int, str]:
    """The card count every candidate must fit (the smallest machine), and the
    card type when the fleet agrees on one."""
    counts = [m.gpu_count for m in machines if m.gpu_count]
    types = {m.gpu_type for m in machines if m.gpu_type}
    return (min(counts) if counts else 0), (types.pop() if len(types) == 1 else "")


def _tp_grid(cards: int, production_tp: int | None) -> list[int]:
    """Tensor-parallel widths worth measuring: the powers of two that fit, kept
    to production's width and its neighbours so the night is spent near what
    runs today rather than on configurations nobody would deploy."""
    widths = [w for w in (1, 2, 4, 8, 16) if not cards or w <= cards]
    if production_tp and production_tp in widths:
        i = widths.index(production_tp)
        widths = widths[max(0, i - 1): i + 2]
    return widths or [1]


def _starter_grid(engine: str, tp_name: str, tp_values: list[int]) -> dict[str, list[Any]]:
    """With no baseline: the tunable parameters whose catalog entry carries an
    example sweep, capped so the product stays one night's work."""
    grid: dict[str, list[Any]] = {tp_name: tp_values}
    for param in catalog.params_for(engine):
        if not param.get("tunable") or param["name"] == tp_name or not param.get("example"):
            continue
        trial = {**grid, param["name"]: list(param["example"])}
        if candidate_count({"grid": trial}) > MAX_DRAFT_CANDIDATES:
            continue
        grid = trial
    return grid


def draft_campaign(
    session: Session, req: DraftRequest, *, dataset_wiring: dict[str, str] | None = None,
) -> Draft:
    """A complete CampaignCreate body, derived from what the platform knows.

    `dataset_wiring` is LLMBench's benchmark -> collection profile map (read by
    the caller, off the database session); None when LLMBench could not be asked.
    """
    settings = get_settings()
    prov: dict[str, str] = {}
    warnings: list[str] = []
    engine = req.engine or "sglang"

    machines, fleet_source = _fleet(session, req)
    cards, card_type = _card_facts(machines)
    if machines:
        prov["machine_names"] = fleet_source
    else:
        warnings.append("no leased machine to size the search for — the tp grid assumes 8 cards")
        cards = 8

    # --- what to serve: the baseline, when there is one -------------------------
    baseline: Baseline | None = None
    if req.baseline_id is not None:
        baseline = session.get(Baseline, req.baseline_id)
        if baseline is None:
            raise LookupError(f"no baseline with id {req.baseline_id}")
    elif req.served_model_name:
        baseline = resolve_baseline(session, req.served_model_name, engine, card_type)

    fields: dict[str, Any]
    if baseline is not None:
        engine = baseline.engine or engine
        fields = LaunchConfig.from_baseline(baseline).as_campaign_fields()
        source = f"baseline #{baseline.id} ({baseline.source or 'entered by hand'})"
        for key in ("engine", "image", "model_path", "served_model_name", "service_port",
                    "extra_env", "extra_volumes"):
            prov[key] = source
        prov["search_space.base"] = f"production engine arguments from {source}"
    else:
        if not req.served_model_name:
            raise ValueError("name a baseline, or a served_model_name to find one by")
        warnings.append(
            f"no baseline for {req.served_model_name!r} on {card_type or 'this fleet'}: "
            "image and model_path are empty — fill them in, or capture a baseline first"
        )
        fields = {"engine": engine, "image": "", "model_path": "",
                  "served_model_name": req.served_model_name, "service_port": 28200,
                  "extra_env": {}, "extra_volumes": {}, "search_space": {"base": {}, "grid": {}}}

    # --- what to try ------------------------------------------------------------------
    tp_name = _tp_param(engine)
    base = dict(fields["search_space"].get("base") or {})
    production_tp = base.pop(tp_name, None)
    try:
        production_tp = int(production_tp) if production_tp is not None else None
    except (TypeError, ValueError):
        production_tp = None
    tp_values = _tp_grid(cards, production_tp)
    if baseline is not None:
        grid = {tp_name: tp_values}
        prov["search_space.grid"] = (
            f"{tp_name} around production's {production_tp}, within {cards} cards"
            if production_tp else f"{tp_name} widths that fit {cards} cards"
        )
    else:
        grid = _starter_grid(engine, tp_name, tp_values)
        prov["search_space.grid"] = f"catalog starter sweep for {engine}, within {cards} cards"
    fields["search_space"] = {"base": base, "grid": grid}

    # --- how to measure and judge -------------------------------------------------------
    screen = req.benchmark_slug or settings.llmbench_benchmark_slug
    prov["benchmark_slug"] = "you chose it" if req.benchmark_slug else "the platform default"
    spec: dict[str, Any] = {
        **fields,
        "name": req.name or f"{fields['served_model_name']} · {card_type or 'fleet'} · "
                            f"{datetime.now(UTC):%Y-%m-%d}",
        "machine_names": [m.name for m in machines] if not req.node_group else [],
        "node_group": req.node_group,
        "benchmark_slug": screen,
        "objective": {"target_metric": DEFAULT_TARGET_METRIC, "direction": "maximize",
                      "redlines": []},
    }
    prov["objective"] = f"platform default: maximize {DEFAULT_TARGET_METRIC}"

    if req.verify_benchmark_slug:
        spec.update(verify_benchmark_slug=req.verify_benchmark_slug, verify_top_k=3,
                    verify_objective={"target_metric": DEFAULT_VERIFY_TARGET_METRIC,
                                      "direction": "maximize", "redlines": []})
        prov["verify_top_k"] = "the three best screened configurations are replayed"
        profile = (dataset_wiring or {}).get(req.verify_benchmark_slug, "")
        if profile:
            spec["dataset_profile"] = profile
            prov["dataset_profile"] = f"what {req.verify_benchmark_slug} replays on LLMBench"
        elif dataset_wiring is None:
            warnings.append("LLMBench could not be asked which dataset the verify benchmark "
                            "replays; set dataset_profile yourself if it uses a rolling one")

    # --- who searches, and when -----------------------------------------------------------
    if req.policy_id is not None:
        spec["policy_id"] = req.policy_id
        spec["policy_settings"] = {}
        prov["policy_settings"] = "the policy's defaults"
    else:
        prov["search"] = "every configuration in the space, in order"

    start = req.daily_start or settings.default_daily_start
    end = req.daily_end or settings.default_daily_end
    if start and end:
        spec.update(daily_start=start, daily_end=end,
                    schedule_timezone=req.schedule_timezone or settings.default_schedule_timezone)
        prov["daily_start"] = "you chose it" if req.daily_start else "the platform's default window"

    n = candidate_count(spec["search_space"])
    if n > MAX_DRAFT_CANDIDATES:
        warnings.append(f"{n} configurations is more than one night usually covers")

    CampaignCreate.model_validate(spec)      # what we hand back must be postable as-is
    return Draft(campaign=spec, provenance=prov, warnings=warnings)
