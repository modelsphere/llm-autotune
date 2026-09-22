"""Tables for a report, rendered once from the ComparisonDocument.

Served next to the data (`format=markdown`), never instead of it. Values are
formatted here so every report shows the same precision and the same signs,
whatever model wrote the prose around them.
"""

from __future__ import annotations

from typing import Any

from app.schemas.agent import ComparisonDocument, ComparisonRun, Delta

_SUMMARY_COLUMNS = (
    ("tpm_card_norm", "Total tok/min/GPU"),
    ("output_tpm_card_norm", "Output tok/min/GPU"),
    ("concurrency", "Concurrency"),
    ("request_output_tps", "Per-request tok/s"),
    ("ttft_ms", "TTFT (ms)"),
)


def fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        if abs(value) >= 1000:
            return f"{value:,.0f}"
        if abs(value) >= 10:
            return f"{value:,.1f}"
        return f"{value:.3g}"
    return str(value)


def fmt_pct(d: Delta | None) -> str:
    if d is None or d.pct is None:
        return "—"
    sign = "+" if d.pct > 0 else ""
    mark = ""
    if d.improved is True:
        mark = " ✅"
    elif d.improved is False:
        mark = " ❌"
    return f"{sign}{d.pct:.1f}%{mark}"


def table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return ""
    head = "| " + " | ".join(headers) + " |"
    sep = "|" + "|".join("---" for _ in headers) + "|"
    body = "\n".join("| " + " | ".join(r) + " |" for r in rows)
    return f"{head}\n{sep}\n{body}"


def _runs(doc: ComparisonDocument) -> list[ComparisonRun]:
    return [doc.baseline, *doc.attempts]


def _passed_cell(r: ComparisonRun) -> str:
    if r.results.status == "measured":
        return fmt(r.results.passed)
    klass = r.results.failure.failure_class if r.results.failure else ""
    return f"failed: {klass}".rstrip(": ")


def headline_table(doc: ComparisonDocument) -> str:
    metric = doc.baseline.results.headline.ranking_metric or "ranking metric"
    headers = [
        "Config", "Engine", "Cards", metric.rsplit(".", 1)[-1],
        "vs baseline", "Peak conc.", "Passed",
    ]
    rows: list[list[str]] = []
    for r in _runs(doc):
        h = r.results.headline
        d = None
        for a in doc.attempts:
            if a.run_id == r.run_id:
                d = a.deltas.headline.get("ranking_value")
        cfg = r.launch.config
        engine = f"{cfg.engine} {r.environment.engine_version or ''}".strip()
        rows.append([
            r.label if r.run_id != doc.baseline.run_id else f"{r.label} (baseline)",
            engine, str(r.launch.cards), fmt(h.ranking_value),
            "—" if r.run_id == doc.baseline.run_id else fmt_pct(d),
            fmt(h.peak_concurrency),
            _passed_cell(r),
        ])
    return table(headers, rows)


def per_gpu_tps(tpm_card_norm: Any) -> float | None:
    """Tokens per second per GPU from a card-normalized TPM (which is per
    minute, scaled to an 8-card machine)."""
    if not isinstance(tpm_card_norm, int | float) or isinstance(tpm_card_norm, bool):
        return None
    return float(tpm_card_norm) / 60.0 / 8.0


def _pct_note(d: Delta | None) -> str:
    if d is None or d.pct is None:
        return ""
    return f" ({'+' if d.pct > 0 else ''}{d.pct:.1f}%)"


def summary_table(doc: ComparisonDocument) -> str:
    """The one results table a reader needs: per scenario, total throughput
    per GPU at the level the gate allowed (and that level), baseline next to
    each attempt with its change; then the ranking metric and the quality
    scores. Everything finer is in the figures."""
    runs = _runs(doc)
    headers = ["", *[r.label for r in runs]]
    rows: list[list[str]] = []

    def delta_of(r: ComparisonRun, pick) -> Delta | None:
        att = next((a for a in doc.attempts if a.run_id == r.run_id), None)
        return pick(att.deltas) if att is not None else None

    for base_s in doc.baseline.results.scenarios:
        cells = []
        for r in runs:
            s = next((x for x in r.results.scenarios if x.key == base_s.key), None)
            if s is None or r.results.status != "measured":
                cells.append("—")
                continue
            tps = per_gpu_tps(s.summary.get("tpm_card_norm"))
            conc = s.summary.get("concurrency")
            level = f" @ c{conc:g}" if isinstance(conc, int | float) else ""
            d = delta_of(r, lambda ds, k=base_s.key: next(
                (x.summary.get("tpm_card_norm") for x in ds.scenarios if x.key == k), None))
            cells.append(f"{fmt(tps)} tok/s/GPU{level}{_pct_note(d)}")
        rows.append([f"{base_s.label} throughput", *cells])

    metric = doc.baseline.results.headline.ranking_metric
    if metric:
        cells = []
        for r in runs:
            d = delta_of(r, lambda ds: ds.headline.get("ranking_value"))
            cells.append(f"{fmt(r.results.headline.ranking_value)}{_pct_note(d)}")
        entry = doc.catalog.get(metric.rsplit(".", 1)[-1])
        rows.append([f"ranked by: {entry.label if entry else metric.rsplit('.', 1)[-1]}",
                     *cells])

    # Quality means the scores something is held to — a floor or a redline —
    # not every number the suite logs about itself (elapsed time, tokens).
    held = {
        f"{v.module}.{v.metric}"
        for v in doc.baseline.results.verdicts
        if v.role in ("redline", "quality_floor")
    }
    for key in doc.baseline.results.headline.quality:
        if key not in held:
            continue
        cells = []
        for r in runs:
            d = delta_of(r, lambda ds, k=key: ds.quality.get(k))
            cells.append(f"{fmt(r.results.headline.quality.get(key))}{_pct_note(d)}")
        rows.append([f"quality: {key.rsplit('.', 1)[-1]}", *cells])

    rows.append(["passed every gate", *[_passed_cell(r) for r in runs]])
    return table(headers, rows)


def scenario_summary_tables(doc: ComparisonDocument) -> dict[str, str]:
    out: dict[str, str] = {}
    keys: list[str] = []
    for r in _runs(doc):
        for s in r.results.scenarios:
            if s.key not in keys:
                keys.append(s.key)
    for key in keys:
        headers = ["Config", *[label for _, label in _SUMMARY_COLUMNS], "Gate"]
        rows = []
        for r in _runs(doc):
            s = next((x for x in r.results.scenarios if x.key == key), None)
            if s is None:
                rows.append([r.label, *["—" for _ in _SUMMARY_COLUMNS], "not run"])
                continue
            rows.append([
                r.label,
                *[fmt(s.summary.get(k)) for k, _ in _SUMMARY_COLUMNS],
                fmt(s.summary.get("meets_gate")),
            ])
        out[key] = table(headers, rows)
    return out


def level_tables(doc: ComparisonDocument) -> dict[str, dict[str, str]]:
    """{scenario key: {metric: table}} — rows are concurrency levels, columns
    are configs, one table per charted metric."""
    out: dict[str, dict[str, str]] = {}
    for s in doc.series:
        levels = sorted({p[0] for line in s.lines for p in line.points})
        by_run = {line.run_id: {p[0]: p[1] for p in line.points} for line in s.lines}
        headers = ["Concurrency", *[line.label for line in s.lines]]
        rows = [
            [str(int(c)), *[fmt(by_run[line.run_id].get(c)) for line in s.lines]] for c in levels
        ]
        unit = f" ({s.unit})" if s.unit else ""
        heading = f"**{s.metric}{unit}**\n\n"
        out.setdefault(s.scenario, {})[s.metric] = heading + table(headers, rows)
    return out


# A replay's rows, in reading order: what it is worth, where the tokens went,
# how fast the first token came (overall, then by prompt length), whether it
# stayed up. A TTFT bucket no request landed in is dropped per table.
_REPLAY_ROWS = (
    "score_card_norm",
    "total_tpm_card_norm",
    "output_tpm_card_norm",
    "uncached_input_tpm",
    "cached_tpm",
    "output_tpm",
    "cache_hit_rate",
    "output_tps_mean",
    "ttft_p50_ms",
    "ttft_p90_ms",
    "ttft_p99_ms",
    *(
        f"ttft_{bucket}_{stat}_ms"
        for bucket in ("lt_6k", "6k_16k", "16k_32k", "32k_64k", "64k_128k", "128k_256k")
        for stat in ("p50", "p90")
    ),
    "total_time_p50_ms",
    "total_time_p99_ms",
    "uptime",
    "unfinished_rate",
    "error_rate",
)


def replay_tables(doc: ComparisonDocument) -> dict[str, str]:
    """{replay scenario key: table} — rows are the replay's metrics, columns
    are the configs, then each attempt's change vs the baseline."""
    out: dict[str, str] = {}
    runs = _runs(doc)
    keys = [s.key for s in doc.baseline.results.scenarios if s.kind == "replay"]
    for key in keys:
        by_run = {
            r.run_id: next((s.metrics for s in r.results.scenarios if s.key == key), {})
            for r in runs
        }
        deltas = {
            a.run_id: next((d.metrics for d in a.deltas.scenarios if d.key == key), {})
            for a in doc.attempts
        }
        headers = [
            "Metric",
            *[r.label for r in runs],
            *[f"{a.label} vs baseline" for a in doc.attempts],
        ]
        rows = []
        for metric in _REPLAY_ROWS:
            values = [by_run[r.run_id].get(metric) for r in runs]
            if all(v is None for v in values):
                continue
            bucket_count = metric.rsplit("_", 2)[0] + "_count"
            if metric.startswith("ttft_") and metric.count("_") > 2 and not any(
                by_run[r.run_id].get(bucket_count) for r in runs
            ):
                continue
            rows.append([
                metric,
                *[fmt(v) for v in values],
                *[fmt_pct(deltas[a.run_id].get(metric)) for a in doc.attempts],
            ])
        out[key] = table(headers, rows)
    return out


def diff_tables(doc: ComparisonDocument) -> dict[int, str]:
    out: dict[int, str] = {}
    for a in doc.attempts:
        d = a.diff_vs_baseline
        rows: list[list[str]] = []
        for name in ("engine", "image", "model_path"):
            change = getattr(d, name)
            if change:
                rows.append([name, fmt(change["from"]), fmt(change["to"])])
        if d.cards:
            rows.append(["cards", fmt(d.cards["from"]), fmt(d.cards["to"])])
        flags = a.launch.rendered.engine_flags
        for k, v in sorted(d.engine_args.get("added", {}).items()):
            rows.append([flags.get(k, k), "—", fmt(v)])
        for k, v in sorted(d.engine_args.get("removed", {}).items()):
            rows.append([flags.get(k, k), fmt(v), "—"])
        for k, v in sorted(d.engine_args.get("changed", {}).items()):
            rows.append([flags.get(k, k), fmt(v["from"]), fmt(v["to"])])
        for k, v in sorted(d.extra_env.get("added", {}).items()):
            rows.append([f"env {k}", "—", fmt(v)])
        for k, v in sorted(d.extra_env.get("removed", {}).items()):
            rows.append([f"env {k}", fmt(v), "—"])
        for k, v in sorted(d.extra_env.get("changed", {}).items()):
            rows.append([f"env {k}", fmt(v["from"]), fmt(v["to"])])
        out[a.run_id] = table(["Setting", "Baseline", a.label], rows) or "_identical to baseline_"
    return out


def launch_blocks(doc: ComparisonDocument) -> dict[int, str]:
    return {
        r.run_id: "```bash\n" + (
            r.launch.rendered.serve_command
            or r.launch.rendered.engine_command
            or "# not recorded"
        ) + "\n```"
        for r in _runs(doc)
    }


def render(doc: ComparisonDocument) -> dict[str, Any]:
    return {
        "headline": headline_table(doc),
        "summary": summary_table(doc),
        "scenarios": scenario_summary_tables(doc),
        "levels": level_tables(doc),
        "replay": replay_tables(doc),
        "diffs": diff_tables(doc),
        "launch": launch_blocks(doc),
    }
