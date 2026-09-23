"""The five parts of a run, built from a loaded RunBundle.

Pure functions: rows in, pydantic out, no session. Each part is the same
object whether served alone or embedded in a document — that is the whole
point of having parts.

Sources, so nobody has to rediscover them:
- launch: the measurement (frozen config) when the run was harvested, else
  the submission, else the campaign + candidate. The docker line is the one
  stored on the run; the engine command comes from the same adapter that
  produced it.
- environment: `Run.env_snapshot` (the in-container version probe) plus the
  machine row.
- benchmark: the measurement's `module_reports` for frozen params, the raw
  LLMBench submission for the full metric_configs, the track for the
  platform's overlay.
- results: the latest llmbench `Result` (metrics + raw), the board's own
  scenario summary, the measurement's verdicts.
"""

from __future__ import annotations

import re
import shlex
from datetime import datetime
from typing import Any

from app.agent.loader import RunBundle
from app.control.engines import get_adapter
from app.control.engines.flags import render_flag
from app.control.launch.base import LaunchSpec, MachineInfo
from app.control.launch_config import LaunchConfig
from app.core.config import get_settings
from app.db.models import RunStatus
from app.evaluation.gate import Gate
from app.evaluation.llmbench import _keyed_runs, module_reports
from app.evaluation.scenarios import (
    is_replay,
    is_scenario,
    scenario_label,
    scenarios_of,
)
from app.objective import target_metric
from app.schemas.agent import (
    BenchmarkLLMBenchOut,
    BenchmarkModuleOut,
    BenchmarkPart,
    BenchmarkPlatformOut,
    EnvironmentPart,
    FailureOut,
    LaunchConfigOut,
    LaunchOrigin,
    LaunchPart,
    LaunchRendered,
    MachineOut,
    ModuleStatusOut,
    QualityOut,
    ResultsHeadline,
    ResultsPart,
    ScenarioLevel,
    ScenarioOut,
    VerdictOut,
)

_LEVEL_KEY = re.compile(r"^c(\d+)$")
_TERMINAL_FAILED = {RunStatus.FAILED.value, RunStatus.KILLED.value}
_RUNNING = {
    RunStatus.LAUNCHING.value,
    RunStatus.WAITING_READY.value,
    RunStatus.HEALTH_CHECK.value,
    RunStatus.BENCHING.value,
    RunStatus.SERVING.value,
}


def links_for(run_id: int) -> dict[str, str]:
    base = f"/api/agent/v1/runs/{run_id}"
    return {
        "self": base,
        "launch": f"{base}/launch",
        "environment": f"{base}/environment",
        "benchmark": f"{base}/benchmark",
        "results": f"{base}/results",
        "raw": f"{base}/results/raw",
        "log": f"{base}/log",
    }


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _as_int(value: Any) -> int | None:
    n = _number(value)
    return int(n) if n is not None else None


def _first_line(text: str | None, limit: int = 300) -> str:
    if not text:
        return ""
    line = str(text).strip().splitlines()[0] if str(text).strip() else ""
    return line[:limit]


def _llmbench_web_base() -> str:
    settings = get_settings()
    if settings.llmbench_web_url:
        return settings.llmbench_web_url.rstrip("/")
    base = (settings.llmbench_base_url or "").rstrip("/")
    return base.removesuffix("/api") if base else ""


# -- launch ------------------------------------------------------------------


def launch_config_of(b: RunBundle) -> LaunchConfig:
    snapshot = dict(b.run.env_snapshot or {})
    if b.campaign is not None:
        args = dict(b.candidate.config or {}) if b.candidate is not None else None
        cfg = LaunchConfig.from_campaign(b.campaign, engine_args=args)
        if b.run.service_port:
            cfg = cfg.model_copy(update={"service_port": int(b.run.service_port)})
    else:
        cfg = LaunchConfig()
    if not cfg.model_path:
        path = b.campaign.model_path if b.campaign is not None else ""
        cfg = cfg.model_copy(update={"model_path": path or ""})
    if not cfg.gpu_type:
        gpu_type = str(snapshot.get("card_type") or "") or (
            b.machine.gpu_type if b.machine is not None else ""
        )
        cfg = cfg.model_copy(update={"gpu_type": gpu_type})
    return cfg


def cards_of(b: RunBundle, cfg: LaunchConfig | None = None) -> int:
    cfg = cfg or launch_config_of(b)
    derived = cfg.cards
    if derived:
        return derived
    return len(b.run.gpu_indices or []) or 1


def _engine_command(b: RunBundle, cfg: LaunchConfig) -> str:
    """The in-container server line, from the engine adapter. Empty when the
    engine is unknown rather than guessed."""
    try:
        spec = LaunchSpec(
            run_id=b.run.id,
            machine=(
                MachineInfo.of(b.machine)
                if b.machine
                else MachineInfo(name="machine", host="0.0.0.0")
            ),
            engine=cfg.engine,
            image=cfg.image,
            model_path=cfg.model_path,
            served_model_name=cfg.served_model_name,
            engine_args=dict(cfg.engine_args or {}),
            gpu_indices=list(b.run.gpu_indices or []),
            port=int(cfg.service_port or b.run.service_port or 28200),
            env=dict(cfg.extra_env or {}),
            volumes=dict(cfg.extra_volumes or {}),
        )
        return shlex.join(get_adapter(cfg.engine).build_command(spec))
    except (ValueError, TypeError):
        return ""


# Parallelism leads a serve command, the way people read one: how many cards
# first, then the tuning.
_PARALLEL_KEYS = ("tp", "tp_size", "tensor_parallel_size", "dp", "dp_size",
                  "data_parallel_size", "pp", "pp_size", "pipeline_parallel_size", "ep",
                  "ep_size")


# Env the platform's container needs and a person's own box does not: the
# NVIDIA runtime's knobs, and the device list the platform assigns.
_CONTAINER_ENV = frozenset({
    "NVIDIA_DISABLE_REQUIRE", "NVIDIA_VISIBLE_DEVICES", "NVIDIA_DRIVER_CAPABILITIES",
    "CUDA_VISIBLE_DEVICES",
})


def serve_command(cfg: LaunchConfig) -> str:
    """The command a person runs to serve this config on their own box:
    `sglang serve` / `vllm serve` against the real weights path, one flag per
    line, env vars in front. Nothing the platform adds to run it (container,
    mounts, host/port, the pod) — those are how we ran it, not the config.
    Empty for an engine we cannot spell."""
    from app.control.engines.flags import negated_flag

    if cfg.engine == "sglang":
        head = ["sglang serve", f"--model-path {shlex.quote(cfg.model_path)}"]
    elif cfg.engine == "vllm":
        head = [f"vllm serve {shlex.quote(cfg.model_path)}"]
    else:
        return ""
    if cfg.served_model_name:
        head.append(f"--served-model-name {shlex.quote(cfg.served_model_name)}")
    args = dict(cfg.engine_args or {})
    ordered = [k for k in _PARALLEL_KEYS if k in args] + sorted(
        k for k in args if k not in _PARALLEL_KEYS
    )
    lines = []
    for key in ordered:
        value = args[key]
        flag = render_flag(cfg.engine, key)
        if isinstance(value, bool):
            if value:
                lines.append(flag)
            elif (negated := negated_flag(cfg.engine, key)) is not None:
                lines.append(negated)
        elif value is not None:
            lines.append(f"{flag} {shlex.quote(str(value))}")
    env = [
        f"{k}={shlex.quote(str(v))}"
        for k, v in sorted((cfg.extra_env or {}).items())
        if k not in _CONTAINER_ENV
    ]
    first = " ".join([*env, head[0]])
    return " \\\n  ".join([first, *head[1:], *lines])


def launch_part(b: RunBundle) -> LaunchPart:
    cfg = launch_config_of(b)
    docker = b.run.launch_command or ""
    origin = LaunchOrigin(
        source="campaign",
        campaign_id=b.campaign.id if b.campaign else None,
        campaign_name=b.campaign.name if b.campaign else "",
        candidate_id=b.candidate.id if b.candidate else None,
    )
    return LaunchPart(
        run_id=b.run.id,
        config=LaunchConfigOut(**cfg.model_dump()),
        cards=cards_of(b, cfg),
        rendered=LaunchRendered(
            docker_command=docker,
            engine_command=_engine_command(b, cfg),
            serve_command=serve_command(cfg),
            engine_flags={k: render_flag(cfg.engine, k) for k in sorted(cfg.engine_args or {})},
        ),
        origin=origin,
    )


def run_label(b: RunBundle) -> str:
    """How a run is named in a comparison: the campaign and the run id."""
    if b.campaign is not None:
        return f"{b.campaign.name} · run {b.run.id}"
    return f"run {b.run.id}"


# -- environment -------------------------------------------------------------


def _duration(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return round((end - start).total_seconds(), 1)


def environment_part(b: RunBundle) -> EnvironmentPart:
    snapshot = dict(b.run.env_snapshot or {})

    def lifted(key: str) -> str | None:
        value = snapshot.get(key)
        return str(value) if value not in (None, "") else None

    return EnvironmentPart(
        run_id=b.run.id,
        machine=MachineOut(
            name=b.machine.name if b.machine else "",
            gpu_type=b.machine.gpu_type if b.machine else "",
            gpu_count=b.machine.gpu_count if b.machine else 0,
            driver=(b.machine.driver if b.machine else "") or "ssh_docker",
        ),
        gpu_indices=list(b.run.gpu_indices or snapshot.get("gpu_indices") or []),
        card_type=lifted("card_type") or lifted("machine_gpu_type"),
        engine_version=lifted("engine_version"),
        torch_version=lifted("torch_version"),
        cuda_version=lifted("cuda_version"),
        gpu_name=lifted("gpu_name"),
        image_digest=lifted("image_digest"),
        started_at=b.run.started_at,
        finished_at=b.run.finished_at,
        duration_seconds=_duration(b.run.started_at, b.run.finished_at),
        snapshot=snapshot,
    )


# -- benchmark -----------------------------------------------------------------


def frozen_reports(b: RunBundle) -> list[dict[str, Any]]:
    """The module verdicts, recomputed from the raw benchmark payload."""
    if b.result is not None and b.result.raw:
        return module_reports(b.result.raw)
    return []


def _raw_runs(b: RunBundle) -> dict[str, dict[str, Any]]:
    raw = (b.result.raw if b.result is not None else None) or {}
    if not isinstance(raw, dict) or not raw.get("runs"):
        return {}
    return {key: run for key, run in _keyed_runs(raw)}


def modules_of(b: RunBundle) -> list[BenchmarkModuleOut]:
    raw_runs = _raw_runs(b)
    out: list[BenchmarkModuleOut] = []
    for index, report in enumerate(frozen_reports(b)):
        key = str(report.get("module") or report.get("module_name") or "")
        params = dict(report.get("params") or {})
        raw_run = raw_runs.get(key) or {}
        configs = raw_run.get("metric_configs_json")
        if not isinstance(configs, list):
            configs = [
                {
                    "key": bound.get("metric"),
                    "role": bound.get("role"),
                    "min_val": bound.get("min"),
                    "max_val": bound.get("max"),
                }
                for bound in report.get("bounds") or []
                if isinstance(bound, dict)
            ]
        out.append(
            BenchmarkModuleOut(
                key=key,
                module_name=str(report.get("module_name") or key.split("#")[0]),
                order_index=int(raw_run.get("order_index", index) or index),
                weight=_number(raw_run.get("weight")),
                # A sweep or a traffic replay: the board's rule, so the two
                # never disagree about what a scenario is.
                is_scenario=is_scenario(report),
                label=scenario_label(report),
                params=params,
                metric_configs=[c for c in configs if isinstance(c, dict)],
            )
        )
    return out


def platform_overlay(campaign) -> BenchmarkPlatformOut:
    """What the PLATFORM says about this benchmark, next to what the benchmark
    itself reported: which metric decides better, and the SLO the objective's
    redlines imply."""
    if campaign is None:
        return BenchmarkPlatformOut()
    gate = Gate.from_objective(campaign.objective)
    return BenchmarkPlatformOut(
        ranking_metric=target_metric(campaign.objective),
        objective=dict(campaign.objective or {}),
        slo=gate.to_dict() if gate else {},
        gate_text=gate.describe() if gate else "",
        max_run_minutes=campaign.max_run_minutes or 0,
    )


def benchmark_part(b: RunBundle) -> BenchmarkPart:
    raw = (b.result.raw if b.result is not None else None) or {}
    slug = (b.campaign.benchmark_slug if b.campaign else "") or ""
    frozen = str(raw.get("benchmark_config_hash") or "")
    web = _llmbench_web_base()
    llmbench_submission = b.run.llmbench_submission_id or ""
    return BenchmarkPart(
        run_id=b.run.id,
        llmbench=BenchmarkLLMBenchOut(
            slug=slug,
            url=f"{web}/benchmarks/{slug}" if web and slug else "",
            submission_id=llmbench_submission or "",
            submission_url=(
                f"{web}/submissions/{llmbench_submission}" if web and llmbench_submission else ""
            ),
            config_hash_frozen=frozen,
            dataset_build_id=(b.campaign.dataset_build_id if b.campaign else "") or "",
        ),
        modules=modules_of(b),
        platform=platform_overlay(b.campaign),
    )


# -- results -----------------------------------------------------------------


def results_status(b: RunBundle) -> str:
    if b.result is not None:
        return "measured"
    status = b.run.status
    if status in _TERMINAL_FAILED or status == RunStatus.SUCCEEDED.value:
        return "failed"
    if status in _RUNNING:
        return "running"
    return "queued"


def ttft_percentile_of(campaign) -> str:
    gate = Gate.from_objective(campaign.objective) if campaign is not None else None
    return (gate.ttft_percentile if gate else "") or "p50"


def gate_of(campaign) -> Gate | None:
    return Gate.from_objective(campaign.objective) if campaign is not None else None


def _meets(level: dict[str, Any], gate: Gate | None, pct: str) -> bool:
    """Did this level meet the SLO: the sweep's own per-level verdict when it
    gave one, else the campaign's gate applied to the level's numbers."""
    flag = level.get("meets_slo")
    if isinstance(flag, bool | int | float) and flag is not None:
        return bool(flag)
    if gate is None:
        return False
    ttft = _number(level.get(f"ttft_{pct}_ms"))
    tps = _number(level.get("request_output_tps"))
    if gate.ttft_ms and (ttft is None or ttft > gate.ttft_ms):
        return False
    return not (gate.min_request_output_tps and (tps is None or tps < gate.min_request_output_tps))


def best_level_of(
    scenario: ScenarioOut, *, cards: int, ttft_percentile: str, gate: Gate | None
) -> dict[str, Any]:
    """The level a reader would pick: the highest total throughput among the
    levels that met the SLO — not necessarily the one the sweep reported,
    which its search confirms at the highest passing concurrency even when a
    lower one served more tokens. Per-GPU rates are the server's rates over
    its cards. A replay has one level, the one it ran at. {} when no level
    passed."""
    per_gpu = max(1, int(cards or 1))
    if scenario.kind == "replay":
        total = _number(scenario.summary.get("tpm_card_norm"))
        output = _number(scenario.summary.get("output_tpm_card_norm"))
        if total is None:
            return {}
        return {
            "concurrency": _number(scenario.summary.get("concurrency")),
            "total_tps_per_gpu": total / 480.0,
            "output_tps_per_gpu": output / 480.0 if output is not None else None,
            "input_tps_per_gpu": (total - output) / 480.0 if output is not None else None,
            "ttft_ms": _number(scenario.summary.get("ttft_ms")),
            "request_output_tps": _number(scenario.summary.get("request_output_tps")),
        }
    passing = [
        lv for lv in scenario.levels
        if _meets(lv.metrics, gate, ttft_percentile)
        and _number(lv.metrics.get("total_tps_mean")) is not None
    ]
    if not passing:
        return {}
    best = max(passing, key=lambda lv: (_number(lv.metrics["total_tps_mean"]), -lv.concurrency))
    m = best.metrics
    total = _number(m.get("total_tps_mean"))
    output = _number(m.get("output_tps_mean"))  # the guidellm sweep's name
    if output is None:
        output = _number(m.get("output_tps"))
    return {
        "concurrency": best.concurrency,
        "total_tps_per_gpu": total / per_gpu,
        "output_tps_per_gpu": output / per_gpu if output is not None else None,
        "input_tps_per_gpu": (total - output) / per_gpu if output is not None else None,
        "ttft_ms": _number(m.get(f"ttft_{ttft_percentile}_ms")),
        "request_output_tps": _number(m.get("request_output_tps")),
    }


def _levels(metrics_json: dict[str, Any]) -> list[ScenarioLevel]:
    levels: list[ScenarioLevel] = []
    for key, value in metrics_json.items():
        match = _LEVEL_KEY.match(str(key))
        if match and isinstance(value, dict):
            levels.append(ScenarioLevel(concurrency=int(match.group(1)), metrics=dict(value)))
    levels.sort(key=lambda lv: lv.concurrency)
    return levels


def _levels_from_flat(metrics: dict[str, Any], key: str) -> list[ScenarioLevel]:
    """The per-concurrency levels of one scenario instance, rebuilt from the flat
    harvested metrics (`<key>.c<N>.<metric>`) when the run's raw LLMBench payload
    does not carry that instance — a row completed from another run's sweep, or
    a raw payload that was not kept. The harvest is what the board ranks on."""
    grouped: dict[int, dict[str, Any]] = {}
    prefix = f"{key}."
    for k, v in metrics.items():
        if not k.startswith(prefix):
            continue
        rest = k[len(prefix):]
        level, _, metric = rest.partition(".")
        match = _LEVEL_KEY.match(level)
        if match and metric and v is not None:
            grouped.setdefault(int(match.group(1)), {})[metric] = v
    return [ScenarioLevel(concurrency=c, metrics=m) for c, m in sorted(grouped.items())]


def _scalars(metrics_json: dict[str, Any]) -> dict[str, Any]:
    return {
        k: v for k, v in metrics_json.items() if not isinstance(v, dict | list) and v is not None
    }


def ranking_metric_of(b: RunBundle) -> str:
    if b.campaign is not None:
        return target_metric(b.campaign.objective)
    return ""


def _quality_floors(campaign) -> dict[str, float]:
    """Redlines on modules that are not scenarios — a quality suite's score,
    say. Read off the campaign objective so a report shows the same floors the
    leaderboard ranked by."""
    floors: dict[str, float] = {}
    objective = (campaign.objective if campaign is not None else None) or {}
    for rule in objective.get("redlines") or []:
        if not isinstance(rule, dict) or rule.get("op") not in (">=", ">"):
            continue
        metric = str(rule.get("metric") or "")
        leaf = metric.rsplit(".", 1)[-1]
        if not metric or leaf.startswith("ttft_") or leaf == "request_output_tps":
            continue  # that is the latency gate, not a quality floor
        try:
            floors[metric] = float(rule.get("value"))
        except (TypeError, ValueError):
            continue
    return floors


def results_part(b: RunBundle) -> ResultsPart:
    status = results_status(b)
    links = links_for(b.run.id)
    metrics: dict[str, Any] = (b.result.metrics if b.result is not None else None) or {}
    reports = frozen_reports(b)
    raw_runs = _raw_runs(b)
    pct = ttft_percentile_of(b.campaign)
    summaries = {s["key"]: s for s in scenarios_of(metrics, reports, pct)}

    scenarios: list[ScenarioOut] = []
    quality: list[QualityOut] = []
    modules: list[ModuleStatusOut] = []
    verdicts: list[VerdictOut] = []
    for report in reports:
        key = str(report.get("module") or report.get("module_name") or "")
        params = dict(report.get("params") or {})
        raw_run = raw_runs.get(key) or {}
        metrics_json = raw_run.get("metrics_json") if isinstance(raw_run, dict) else None
        metrics_json = metrics_json if isinstance(metrics_json, dict) else {}
        modules.append(
            ModuleStatusOut(
                key=key,
                module_name=str(report.get("module_name") or key.split("#")[0]),
                status=str(report.get("status") or ""),
                passed=report.get("passed") if isinstance(report.get("passed"), bool) else None,
                score=_number(report.get("score")),
                error=_first_line(report.get("error")),
            )
        )
        for bound in report.get("bounds") or []:
            if not isinstance(bound, dict):
                continue
            verdicts.append(
                VerdictOut(
                    module=key,
                    metric=str(bound.get("metric") or ""),
                    role=str(bound.get("role") or "redline"),
                    min=_number(bound.get("min")),
                    max=_number(bound.get("max")),
                    actual=_number(bound.get("actual")),
                    ok=bound.get("ok") if isinstance(bound.get("ok"), bool) else None,
                )
            )
        if is_scenario(report):
            summary = dict(summaries.get(key) or {})
            for drop in ("key", "kind", "label", "input_tokens", "output_tokens"):
                summary.pop(drop, None)
            # The flat dict under the raw payload's own scalars: the raw one is
            # what LLMBench sent, the flat one also carries what the harvest
            # derived from it (a replay's `score_card_norm`), and it is all
            # there is when the raw payload was not kept.
            module_scalars = {
                **{
                    k.split(".", 1)[1]: v
                    for k, v in metrics.items()
                    if k.startswith(f"{key}.") and k.count(".") == 1
                },
                **_scalars(metrics_json),
            }
            scenarios.append(
                ScenarioOut(
                    key=key,
                    kind="replay" if is_replay(report) else "sweep",
                    label=scenario_label(report),
                    input_tokens=_as_int(params.get("input_tokens")),
                    output_tokens=_as_int(params.get("output_tokens")),
                    summary=summary,
                    metrics=module_scalars,
                    levels=_levels(metrics_json) or _levels_from_flat(metrics, key),
                )
            )
            scenarios[-1].best_level = best_level_of(
                scenarios[-1], cards=cards_of(b), ttft_percentile=pct, gate=gate_of(b.campaign)
            )
        else:
            scores = {
                k: float(v)
                for k, v in (_scalars(metrics_json) or {
                    k.split(".", 1)[1]: v
                    for k, v in metrics.items()
                    if k.startswith(f"{key}.") and k.count(".") == 1
                }).items()
                if _number(v) is not None
            }
            quality.append(
                QualityOut(
                    key=key,
                    module_name=str(report.get("module_name") or key.split("#")[0]),
                    scores=scores,
                    passed=report.get("passed") if isinstance(report.get("passed"), bool) else None,
                    error=_first_line(report.get("error")),
                )
            )

    # Quality floors the campaign's objective declares as redlines on a
    # non-scenario module, which the benchmark itself may not have judged.
    floors = _quality_floors(b.campaign)
    for full_key, floor in floors.items():
        module, _, metric = str(full_key).partition(".")
        actual = _number(metrics.get(full_key))
        limit = _number(floor)
        verdicts.append(
            VerdictOut(
                module=module,
                metric=metric or full_key,
                role="quality_floor",
                min=limit,
                actual=actual,
                ok=(actual >= limit) if (actual is not None and limit is not None) else None,
            )
        )

    first = scenarios[0] if scenarios else None
    ranking_metric = ranking_metric_of(b)
    headline = ResultsHeadline(
        ranking_metric=ranking_metric,
        ranking_value=_number(metrics.get(ranking_metric)) if ranking_metric else None,
        total_tpm_card_norm=_number(first.summary.get("tpm_card_norm")) if first else None,
        output_tpm_card_norm=_number(first.summary.get("output_tpm_card_norm")) if first else None,
        peak_concurrency=_number(metrics.get(f"{first.key}.peak_concurrency")) if first else None,
        reported_concurrency=_number(first.summary.get("concurrency")) if first else None,
        meets_gate=first.summary.get("meets_gate") if first else None,
        quality={f"{q.key}.{k}": v for q in quality for k, v in q.scores.items()},
    )

    failure = None
    if status == "failed":
        failure = FailureOut(
            failure_class=b.run.failure_class or "",
            error=_first_line(b.run.error) or "no benchmark result was recorded for this run",
            run_status=b.run.status,
            log=links["log"],
        )
    result = b.result
    return ResultsPart(
        run_id=b.run.id,
        status=status,  # type: ignore[arg-type]
        passed=result.passed if result is not None else None,
        feasible=bool(result.feasible) if result is not None else None,
        score=result.score if result is not None else None,
        objective_value=result.objective_value if result is not None else None,
        headline=headline,
        scenarios=scenarios,
        quality=quality,
        verdicts=verdicts,
        modules=modules,
        metrics=metrics,
        failure=failure,
        links={"raw": links["raw"], "log": links["log"]},
    )
