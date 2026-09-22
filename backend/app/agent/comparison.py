"""Baseline against attempts: the arithmetic a report is made of.

The agent never computes a percentage or decides whether two runs may be
compared. Both happen here, once, from the parts. Attempt order is the
ablation order the caller chose: every attempt is diffed against the baseline
and against the attempt before it, so both a "baseline vs optimized" table
and a "one knob at a time" narrative read straight off the document.
"""

from __future__ import annotations

from typing import Any

from app import metrics_catalog
from app.agent.loader import RunBundle
from app.agent.parts import (
    benchmark_part,
    environment_part,
    launch_part,
    results_part,
    run_label,
)
from app.schemas.agent import (
    BestOut,
    CatalogEntry,
    ComparisonAttempt,
    ComparisonDocument,
    ComparisonRun,
    ConfigDiff,
    Delta,
    Deltas,
    NotComparableReason,
    ResultsPart,
    ScenarioDeltas,
    Series,
    SeriesLine,
    VerdictChange,
)

# Module params that define what a scenario measured. Two runs whose scenario
# modules differ in any of these measured different things.
SCENARIO_PARAMS = (
    "input_tokens",
    "output_tokens",
    "concurrencies",
    "requests_per_concurrency",
    "slo_max_ttft_ms",
    "slo_ttft_percentile",
    "slo_min_request_output_tps",
    "dataset_path",
    # a traffic replay: what it replays, how much of it, how hard
    "dataset_source",
    "dataset_profile",
    "max_samples",
    "concurrency",
    "max_generation_tokens",
)

# Per-concurrency metrics charted by default. Overridable per request; only
# the ones that actually appear in the levels are emitted.
DEFAULT_SERIES_METRICS = (
    "output_tps_mean",  # what the guidellm sweep calls it per level
    "output_tps",  # what a plain module calls it
    "request_output_tps",
    "total_tps_mean",
    "ttft_p50_ms",
    "ttft_p99_ms",
    "tpot_p50_ms",
    "tpot_p99_ms",
    "itl_p50_ms",
    "itl_p99_ms",
)

_LOWER_HINTS = (
    "_ms", "latency", "ttft", "tpot", "itl", "fail", "error", "http_status_4", "http_status_5",
    "unfinished", "wall_time",
)
# Moved, but not better or worse: how much was sent, how many landed in a
# bucket, which dataset it was. A replay reports dozens of these.
_NEUTRAL_HINTS = (
    "concurrency", "http_status_200", "measured_requests", "duration", "search_",
    "_count", "_requests", "_tokens", "dataset_",
)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def direction_of(metric: str) -> str:
    """Which way is better for a metric, from the catalog when it knows the
    key, from the name otherwise. Unknown names default to higher."""
    bare = metric.rsplit(".", 1)[-1]
    for spec in metrics_catalog.METRICS:
        if spec["key"].rsplit(".", 1)[-1] == bare:
            return spec["better"]
    if any(h in bare for h in _LOWER_HINTS):
        return "lower"
    return "higher"


def catalog_entry(metric: str) -> CatalogEntry:
    bare = metric.rsplit(".", 1)[-1]
    spec = next((m for m in metrics_catalog.METRICS if m["key"].rsplit(".", 1)[-1] == bare), None)
    if spec is not None:
        return CatalogEntry(
            key=bare, label=spec["label"], unit=spec.get("unit", ""),
            better=spec["better"], help=spec.get("help", ""),
        )
    unit = "ms" if bare.endswith("_ms") else ("tok/s" if "tps" in bare else "")
    label = bare.replace("_ms", "").replace("_", " ")
    return CatalogEntry(key=bare, label=label, unit=unit, better=direction_of(bare))


def delta(baseline: Any, attempt: Any, metric: str) -> Delta | None:
    b, a = _num(baseline), _num(attempt)
    if b is None and a is None:
        return None
    better = direction_of(metric)
    pct = None
    improved = None
    if b is not None and a is not None:
        if b != 0:
            pct = round((a - b) / abs(b) * 100.0, 2)
        if a != b and not any(h in metric for h in _NEUTRAL_HINTS):
            improved = (a > b) if better == "higher" else (a < b)
    return Delta(baseline=b, attempt=a, pct=pct, better=better, improved=improved)


def _dict_deltas(base: dict[str, Any], att: dict[str, Any]) -> dict[str, Delta]:
    out: dict[str, Delta] = {}
    for key in sorted(set(base) | set(att)):
        d = delta(base.get(key), att.get(key), key)
        if d is not None:
            out[key] = d
    return out


def deltas_between(base: ResultsPart, att: ResultsPart) -> Deltas:
    headline = _dict_deltas(
        base.headline.model_dump(exclude={"ranking_metric", "quality", "meets_gate"}),
        att.headline.model_dump(exclude={"ranking_metric", "quality", "meets_gate"}),
    )
    scenarios: list[ScenarioDeltas] = []
    att_scenarios = {s.key: s for s in att.scenarios}
    for bs in base.scenarios:
        a_s = att_scenarios.get(bs.key)
        if a_s is None:
            continue
        levels = []
        a_levels = {lv.concurrency: lv.metrics for lv in a_s.levels}
        for lv in bs.levels:
            if lv.concurrency in a_levels:
                levels.append({
                    "concurrency": lv.concurrency,
                    "metrics": _dict_deltas(lv.metrics, a_levels[lv.concurrency]),
                })
        scenarios.append(
            ScenarioDeltas(
                key=bs.key,
                label=bs.label,
                summary=_dict_deltas(bs.summary, a_s.summary),
                best_level=_dict_deltas(bs.best_level, a_s.best_level),
                levels=levels,
                # A replay has no levels; what it measured beyond the summary
                # (score, cached vs uncached TPM, TTFT by input length) is
                # its module-level metrics, so those get deltas too.
                metrics=(
                    _dict_deltas(bs.metrics, a_s.metrics) if bs.kind == "replay" else {}
                ),
            )
        )
    return Deltas(
        headline=headline,
        scenarios=scenarios,
        quality=_dict_deltas(base.headline.quality, att.headline.quality),
    )


# -- config diff -----------------------------------------------------------------


def _dict_diff(base: dict[str, Any], att: dict[str, Any]) -> dict[str, Any]:
    added = {k: att[k] for k in att if k not in base}
    removed = {k: base[k] for k in base if k not in att}
    changed = {k: {"from": base[k], "to": att[k]} for k in base if k in att and base[k] != att[k]}
    return {"added": added, "removed": removed, "changed": changed}


def config_diff(base: ComparisonRun, att: ComparisonRun) -> ConfigDiff:
    b, a = base.launch.config, att.launch.config

    def scalar(name: str) -> dict[str, Any] | None:
        bv, av = getattr(b, name), getattr(a, name)
        return {"from": bv, "to": av} if bv != av else None

    args = _dict_diff(b.engine_args, a.engine_args)
    env = _dict_diff(b.extra_env, a.extra_env)
    cards = None
    if base.launch.cards != att.launch.cards:
        cards = {"from": base.launch.cards, "to": att.launch.cards}
    diff = ConfigDiff(
        engine=scalar("engine"),
        image=scalar("image"),
        model_path=scalar("model_path"),
        engine_args=args,
        extra_env=env,
        cards=cards,
    )
    diff.changed = any([
        diff.engine, diff.image, diff.model_path, cards,
        args["added"], args["removed"], args["changed"],
        env["added"], env["removed"], env["changed"],
    ])
    return diff


def verdict_changes(base: ResultsPart, att: ResultsPart) -> list[VerdictChange]:
    # Keyed by role too: the same metric can be both an LLMBench redline and a
    # platform quality floor, with different limits and different verdicts.
    b = {(v.module, v.metric, v.role): v.ok for v in base.verdicts}
    a = {(v.module, v.metric, v.role): v.ok for v in att.verdicts}
    return [
        VerdictChange(
            module=m, metric=k, role=role, baseline_ok=b.get((m, k, role)),
            attempt_ok=a.get((m, k, role)),
        )
        for (m, k, role) in sorted(set(b) | set(a))
        if b.get((m, k, role)) != a.get((m, k, role))
    ]


# -- comparability -------------------------------------------------------------


# A replay's frozen `dataset_path` is the per-submission pin LLMBench copied
# the build to (`pins/s1286-r2701__<build>__<ts>.jsonl.gz`): two runs of the
# same build never share it. What they replayed is compared by build id
# (`dataset_differs`) instead.
_REPLAY_UNCOMPARED = frozenset({"dataset_path"})


def _scenario_params(bundle_benchmark) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for m in bundle_benchmark.modules:
        if not m.is_scenario:
            continue
        skip = _REPLAY_UNCOMPARED if m.module_name.startswith("replay") else frozenset()
        out[m.key] = {
            p: m.params.get(p) for p in SCENARIO_PARAMS if p in m.params and p not in skip
        }
    return out


def comparability(baseline: RunBundle, attempts: list[RunBundle]) -> list[NotComparableReason]:
    reasons: list[NotComparableReason] = []
    base_bench = benchmark_part(baseline)
    base_params = _scenario_params(base_bench)
    base_status = results_part(baseline).status
    if base_status not in ("measured", "failed"):
        reasons.append(NotComparableReason(
            code="run_not_finished", run_id=baseline.run.id, detail=f"baseline is {base_status}",
        ))
    for att in attempts:
        rid = att.run.id
        b_cfg, a_cfg = launch_part(baseline).config, launch_part(att).config
        if b_cfg.served_model_name != a_cfg.served_model_name:
            reasons.append(NotComparableReason(
                code="model_differs", run_id=rid, field="served_model_name",
                baseline=b_cfg.served_model_name, attempt=a_cfg.served_model_name,
            ))
        if b_cfg.gpu_type and a_cfg.gpu_type and b_cfg.gpu_type != a_cfg.gpu_type:
            reasons.append(NotComparableReason(
                code="gpu_type_differs", run_id=rid, field="gpu_type",
                baseline=b_cfg.gpu_type, attempt=a_cfg.gpu_type,
            ))
        att_bench = benchmark_part(att)
        if base_bench.llmbench.slug != att_bench.llmbench.slug:
            reasons.append(NotComparableReason(
                code="benchmark_differs", run_id=rid, field="benchmark_slug",
                baseline=base_bench.llmbench.slug, attempt=att_bench.llmbench.slug,
            ))
        b_ds, a_ds = base_bench.llmbench.dataset_build_id, att_bench.llmbench.dataset_build_id
        if b_ds and a_ds and b_ds != a_ds:
            reasons.append(NotComparableReason(
                code="dataset_differs", run_id=rid, field="dataset_build_id",
                baseline=b_ds, attempt=a_ds,
            ))
        att_params = _scenario_params(att_bench)
        # Only modules both runs actually ran can disagree; a failed attempt
        # that never reached the benchmark has no frozen params to compare.
        for key in sorted(set(base_params) & set(att_params)):
            for field in SCENARIO_PARAMS:
                bv, av = base_params[key].get(field), att_params[key].get(field)
                if bv is not None and av is not None and str(bv) != str(av):
                    reasons.append(NotComparableReason(
                        code="module_params_differ", run_id=rid, module=key, field=field,
                        baseline=bv, attempt=av,
                    ))
        status = results_part(att).status
        if status not in ("measured", "failed"):
            reasons.append(NotComparableReason(
                code="run_not_finished", run_id=rid, detail=f"attempt is {status}",
            ))
    return reasons


# -- best + series -----------------------------------------------------------------


def _ranked_value(run: ComparisonRun) -> float | None:
    r = run.results
    if r.status != "measured":
        return None
    return r.headline.ranking_value


def best_of(baseline: ComparisonRun, attempts: list[ComparisonAttempt]) -> BestOut:
    runs: list[ComparisonRun] = [baseline, *attempts]
    ranking_metric = baseline.results.headline.ranking_metric or next(
        (a.results.headline.ranking_metric for a in attempts if a.results.headline.ranking_metric),
        "",
    )
    better = direction_of(ranking_metric) if ranking_metric else "higher"
    # Passed configs first: a faster service that failed a redline is not a win.
    candidates = [(r, _ranked_value(r)) for r in runs if _ranked_value(r) is not None]
    passed = [(r, v) for r, v in candidates if r.results.passed]
    pool = passed or candidates
    overall = None
    if pool:
        pick = (max if better == "higher" else min)(pool, key=lambda rv: rv[1])
        overall = {
            "run_id": pick[0].run_id, "label": pick[0].label,
            "by": ranking_metric, "value": pick[1], "passed_only": bool(passed),
        }
    per_scenario = []
    keys: list[str] = []
    for r in runs:
        for s in r.results.scenarios:
            if s.key not in keys:
                keys.append(s.key)
    for key in keys:
        pool_s = []
        for r in runs:
            s = next((x for x in r.results.scenarios if x.key == key), None)
            if s is None:
                continue
            v = _num(s.summary.get("output_tpm_card_norm")) or _num(s.summary.get("tpm_card_norm"))
            if v is not None:
                pool_s.append((r, v, bool(s.summary.get("passed"))))
        passed_s = [p for p in pool_s if p[2]] or pool_s
        if passed_s:
            pick = max(passed_s, key=lambda p: p[1])
            per_scenario.append({
                "key": key, "run_id": pick[0].run_id, "label": pick[0].label,
                "by": "output_tpm_card_norm", "value": pick[1],
            })
    return BestOut(overall=overall, per_scenario=per_scenario)


def series_of(
    baseline: ComparisonRun, attempts: list[ComparisonAttempt], metrics: tuple[str, ...]
) -> list[Series]:
    runs: list[ComparisonRun] = [baseline, *attempts]
    out: list[Series] = []
    seen: list[tuple[str, str]] = []
    for r in runs:
        for s in r.results.scenarios:
            for metric in metrics:
                if (s.key, metric) in seen:
                    continue
                lines: list[SeriesLine] = []
                for rr in runs:
                    sc = next((x for x in rr.results.scenarios if x.key == s.key), None)
                    if sc is None:
                        continue
                    points = [
                        [float(lv.concurrency), _num(lv.metrics.get(metric))]
                        for lv in sc.levels
                        if _num(lv.metrics.get(metric)) is not None
                    ]
                    if points:
                        lines.append(SeriesLine(run_id=rr.run_id, label=rr.label, points=points))
                if lines:
                    entry = catalog_entry(metric)
                    out.append(Series(
                        scenario=s.key, scenario_label=s.label, metric=metric,
                        unit=entry.unit, better=entry.better, lines=lines,
                    ))
                seen.append((s.key, metric))
    return out


def catalog_for(doc_runs: list[ComparisonRun], series: list[Series]) -> dict[str, CatalogEntry]:
    names: set[str] = set()
    for r in doc_runs:
        h = r.results.headline
        if h.ranking_metric:
            names.add(h.ranking_metric.rsplit(".", 1)[-1])
        skip = ("ranking_metric", "ranking_value", "quality", "meets_gate")
        names.update(k for k in h.model_dump() if k not in skip)
        for s in r.results.scenarios:
            names.update(s.summary)
            for lv in s.levels:
                names.update(lv.metrics)
    names.update(s.metric for s in series)
    return {n: catalog_entry(n) for n in sorted(names) if n}


# -- the document -------------------------------------------------------------------


def comparison_run(b: RunBundle) -> ComparisonRun:
    return ComparisonRun(
        run_id=b.run.id,
        label=run_label(b),
        launch=launch_part(b),
        environment=environment_part(b),
        results=results_part(b),
    )


def build_comparison(
    baseline: RunBundle,
    attempts: list[RunBundle],
    *,
    reasons: list[NotComparableReason],
    series_metrics: tuple[str, ...] = DEFAULT_SERIES_METRICS,
) -> ComparisonDocument:
    base = comparison_run(baseline)
    rows: list[ComparisonAttempt] = []
    previous: ComparisonRun = base
    for position, bundle in enumerate(attempts, start=1):
        run = comparison_run(bundle)
        rows.append(
            ComparisonAttempt(
                **run.model_dump(),
                position=position,
                diff_vs_baseline=config_diff(base, run),
                diff_vs_previous=config_diff(previous, run),
                deltas=deltas_between(base.results, run.results),
                deltas_vs_previous=deltas_between(previous.results, run.results),
                verdict_changes=verdict_changes(base.results, run.results),
            )
        )
        previous = run
    series = series_of(base, rows, series_metrics)
    ids = ",".join(str(a.run_id) for a in rows)
    return ComparisonDocument(
        comparable=not reasons,
        reasons=reasons,
        benchmark=benchmark_part(baseline),
        baseline=base,
        attempts=rows,
        best=best_of(base, rows),
        series=series,
        catalog=catalog_for([base, *rows], series),
        links={
            "self": f"/api/agent/v1/comparison?baseline={base.run_id}&attempts={ids}",
            "markdown": (
                f"/api/agent/v1/comparison?baseline={base.run_id}&attempts={ids}&format=markdown"
            ),
            "baseline": f"/api/agent/v1/runs/{base.run_id}",
            **{f"attempt_{a.position}": f"/api/agent/v1/runs/{a.run_id}" for a in rows},
        },
    )
