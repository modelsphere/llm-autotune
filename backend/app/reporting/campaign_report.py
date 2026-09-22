"""The morning report — what the design calls the replacement for the Google
doc a human used to fill in by hand.

Answers the questions someone actually has after a night: did anything beat
what we already run, what broke and why, and what should I do about it.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.control.search.space import swept_keys
from app.core.config import get_settings
from app.db.models import (
    Campaign,
    Machine,
    Result,
    Run,
    RunKind,
    is_baseline_candidate,
)
from app.evaluation.aggregate import summary_of
from app.objective import (
    direction,
    improvement_pct,
    sort_key,
    target_metric,
)
from app.staging import SCREEN, VERIFY
from app.staging import objective as stage_objective


@dataclass
class _ConfigGroup:
    """Every measurement of one configuration.

    Without top-K confirmation this is always a group of one and the report
    reads exactly as it did before. With it, `n` and `spread_pct` are what
    separate "faster" from "faster that night".
    """

    config: dict
    runs: list[Run]
    values: list[float]
    breaches: list[str]

    @property
    def n(self) -> int:
        return len(self.values)

    @property
    def mean(self) -> float:
        return sum(self.values) / len(self.values)

    @property
    def spread_pct(self) -> float | None:
        """Full range as a percentage of the mean. Deliberately range, not
        stdev: at three samples a standard deviation implies a precision the
        sample size does not support, and the worst case is what decides
        whether a lead is real."""
        if self.n < 2 or not self.mean:
            return None
        return (max(self.values) - min(self.values)) / abs(self.mean) * 100


def _evidence_note(group: "_ConfigGroup") -> str:
    """How much measurement is behind a headline claim."""
    if group.n < 2:
        return ", on a single measurement"
    spread = group.spread_pct
    tail = f", spread {spread:.1f}%" if spread is not None else ""
    return f", confirmed over {group.n} measurements{tail}"


def _config_key(config: dict) -> str:
    return json.dumps(config, sort_keys=True, separators=(",", ":"), default=str)


def _group_by_config(
    runs: list[Run],
    score: Callable[[Run], float | None],
    crossed: Callable[[Run], list[str]],
) -> list[_ConfigGroup]:
    """Collapse repeated measurements of the same configuration.

    A group carries a breach if ANY of its measurements breached. That is the
    whole point of repeating: node-24 produced a config that passed every
    functional check on one run and regressed on another at identical
    settings, and averaging that away would promote it.
    """
    grouped: dict[str, _ConfigGroup] = {}
    for run in runs:
        config = getattr(run.candidate, "config", {}) or {}
        group = grouped.setdefault(
            _config_key(config), _ConfigGroup(config, [], [], [])
        )
        group.runs.append(run)
        value = score(run)
        if value is not None:
            group.values.append(value)
        for breach in crossed(run):
            if breach not in group.breaches:
                group.breaches.append(breach)
    return [g for g in grouped.values() if g.values]


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:,.1f}"
    return str(value)


def _varying_keys(runs: list[Run]) -> set[str]:
    """Keys whose value differs between candidates — the search dimensions.

    A campaign's config is mostly a fixed base (context length, parsers, cache
    settings) with one or two knobs moving. Printing all of it makes the table
    unreadable and hides the very thing being compared.
    """
    seen: dict[str, set[str]] = {}
    for run in runs:
        if is_baseline_candidate(getattr(run, "candidate", None)):
            continue  # production's config is not one of the swept points
        config = getattr(run.candidate, "config", {}) or {}
        for key, value in config.items():
            seen.setdefault(key, set()).add(repr(value))
    return {key for key, values in seen.items() if len(values) > 1}


def _swept_keys(campaign: Campaign, runs: list[Run]) -> set[str]:
    """Which parameters this campaign varies.

    The space the campaign was created with is the source of truth. Diffing the
    candidates is only a fallback for campaigns that predate a declared space —
    as a primary source it disagrees with the declaration whenever two points
    happen to render the same value.
    """
    declared = swept_keys(getattr(campaign, "search_space", None) or {})
    return set(declared) if declared else _varying_keys(runs)


def _config_summary(config: dict, varying: set[str] | None = None) -> str:
    if "__baseline__" in config:
        return "production (as handed over)"
    items = {k: v for k, v in config.items() if not varying or k in varying}
    if not items:  # single-candidate campaign: nothing varies, show everything
        items = config
    return ", ".join(f"{k}={v}" for k, v in sorted(items.items())) or "(defaults)"


def _stage(run: Run) -> str:
    """Tolerant of the plain objects the report tests build, which predate
    staging and are all screening runs."""
    return getattr(run, "stage", SCREEN)


def _verify_objective(campaign: Campaign) -> dict:
    if not getattr(campaign, "verify_benchmark_slug", ""):
        return {}
    return stage_objective(campaign, VERIFY)


def _dataset_line(campaign: Campaign) -> list[str]:
    """Which traffic sample these numbers came from.

    Worth a line of its own because it is the one fact that decides whether
    this table can be compared to another campaign's at all. A build id and
    its window are enough to answer that; without them, two nights' scores
    look equally authoritative whether or not they measured the same traffic.
    """
    build = getattr(campaign, "dataset_build_id", "") or ""
    profile = getattr(campaign, "dataset_profile", "") or ""
    if not build:
        if profile:
            return ["", f"Dataset: `{profile}`, not pinned — see the campaign events.", ""]
        return [""]
    applied = getattr(campaign, "dataset_policy_applied", "") or ""
    note = " (adopted from a campaign already using it)" if applied == "adopted" else ""
    return ["", f"Replayed `{profile}` build `{build}`{note}.", ""]


def _verification_section(
    campaign: Campaign,
    verified: list[Run],
    results_by_run: dict[int, Result],
    varying: set[str],
) -> list[str]:
    """What the expensive benchmark said, on its own terms.

    Kept apart from the screening leaderboard rather than merged into it: the
    two stages ran different benchmarks, so their scores share no scale and no
    units. One table sorted across both would be a ranking of nothing.

    There is deliberately no "vs production" column. The baseline canary
    measures the handed-over service with the SCREENING benchmark, so a
    percentage against it here would be computed from two different
    measurements of two different workloads — a number that looks like a
    finding and is not one.
    """
    objective = _verify_objective(campaign)
    target = target_metric(objective)
    slug = getattr(campaign, "verify_benchmark_slug", "") or "the second benchmark"

    def score(run: Run) -> float | None:
        return summary_of(results_by_run.get(run.id), objective).objective_value

    def crossed(run: Run) -> list[str]:
        return summary_of(results_by_run.get(run.id), objective).breaches

    scored = [r for r in verified if r.status == "succeeded" and score(r) is not None]
    groups = _group_by_config(scored, score, crossed)
    ranked = sorted(
        [g for g in groups if not g.breaches], key=lambda g: sort_key(g.mean, objective)
    )
    rejected = [g for g in groups if g.breaches]

    lines = [f"## Verified on `{slug}`", *_dataset_line(campaign)]
    if not groups:
        attempted = len(verified)
        lines += [
            f"{attempted} candidate(s) were sent to the replay benchmark and none "
            "produced a usable measurement — see Failures below."
            if attempted
            else "Nothing reached this stage yet.",
            "",
        ]
        return lines

    if ranked:
        best = ranked[0]
        lines += [
            f"**{_config_summary(best.config, varying)}** leads on real traffic "
            f"({_fmt(best.mean)} {target}){_evidence_note(best)}.",
            "",
            "Production was measured on the screening benchmark, not this one, "
            "so there is no like-for-like comparison to quote here.",
            "",
        ]
    else:
        lines += [
            "Every verified candidate crossed a redline on real traffic — the "
            "screening leaderboard below did not predict that.",
            "",
        ]

    lines += [f"| Config | {target} | Measurements | Runs |", "|---|---|---|---|"]
    for group in [*ranked, *rejected]:
        note = " ⚠ " + "; ".join(group.breaches) if group.breaches else ""
        lines.append(
            f"| {_config_summary(group.config, varying)}{note} | {_fmt(group.mean)} | "
            f"{group.n} | {', '.join(str(r.id) for r in group.runs)} |"
        )
    lines.append("")
    return lines


def render_campaign_report(
    campaign: Campaign,
    runs: list[Run],
    results_by_run: dict[int, Result],
    machines: dict[int, Machine],
) -> str:
    """Markdown. Deliberately plain — this gets pasted into chat and tickets."""
    target = target_metric(campaign.objective)
    varying = _swept_keys(campaign, runs)
    noise_pct = get_settings().report_noise_threshold_pct

    baseline_runs = [r for r in runs if r.kind == RunKind.BASELINE.value]
    all_experiments = [r for r in runs if r.kind != RunKind.BASELINE.value]
    # Everything below this line ranks the SCREENING stage. Verification is
    # reported separately, above, because it is a different measurement.
    verified = [r for r in all_experiments if _stage(r) == VERIFY]
    experiments = [r for r in all_experiments if _stage(r) == SCREEN]
    succeeded = [r for r in experiments if r.status == "succeeded"]
    failed = [r for r in all_experiments if r.status in ("failed", "killed")]

    baseline_value = None
    if baseline_runs:
        baseline_value = summary_of(
            results_by_run.get(baseline_runs[0].id), campaign.objective
        ).objective_value

    def score(run: Run) -> float | None:
        """The objective value the run was judged by — resolved when the
        result landed, so a campaign whose objective was edited afterwards
        still reports the ranking the night actually ran under."""
        return summary_of(results_by_run.get(run.id), campaign.objective).objective_value

    def crossed_redlines(run: Run) -> list[str]:
        return summary_of(results_by_run.get(run.id), campaign.objective).breaches

    scored = [r for r in succeeded if score(r) is not None]
    # One row per CONFIG, not per run: with top-K confirmation the same config
    # is measured several times, and listing it three times would read as three
    # different results that happen to agree.
    groups = _group_by_config(scored, score, crossed_redlines)
    # A config that wins the target while breaching a declared SLO is not the
    # answer — it is a rejected option, and must never lead the report.
    eligible = [g for g in groups if not g.breaches]
    rejected = [g for g in groups if g.breaches]
    ranked = sorted(eligible, key=lambda g: sort_key(g.mean, campaign.objective))
    repeated = any(g.n > 1 for g in groups)

    lines: list[str] = [
        f"# {campaign.name}",
        "",
        f"- **Objective**: {direction(campaign.objective)} `{target}`",
        f"- **Model**: `{campaign.model_path}` on `{campaign.engine}` "
        f"(`{campaign.image}`)",
        f"- **Runs**: {len(succeeded)} succeeded, {len(failed)} failed"
        f"{f', {len(verified)} verified' if verified else ''}"
        f"{', 1 baseline' if baseline_runs else ''}",
        "",
    ]

    # The expensive stage goes first when there is one: it is the answer, and
    # everything below it is the shortlisting that led there.
    if verified:
        lines += _verification_section(campaign, verified, results_by_run, varying)

    # -- headline ------------------------------------------------------------
    verdict_heading = "## Screening verdict" if verified else "## Verdict"
    results_heading = (
        "## Screening results (cheap benchmark)" if verified else "## Results"
    )
    if ranked and baseline_value:
        best = ranked[0]
        delta = improvement_pct(best.mean, baseline_value, campaign.objective) or 0.0
        config_label = _config_summary(best.config, varying)
        numbers = f"({_fmt(best.mean)} vs {_fmt(baseline_value)})"
        evidence = _evidence_note(best)
        if abs(delta) < noise_pct:
            # Reporting a sub-noise difference as a win is how tuning theatre
            # starts. Two identical configs measured 0.2% apart on this rig.
            verdict = (
                f"**No meaningful difference.** The best candidate "
                f"(**{config_label}**) is {delta:+.1f}% against production "
                f"{numbers} — inside the ±{noise_pct:g}% noise band "
                f"for a single benchmark, so it is not evidence of an improvement."
            )
        elif best.spread_pct is not None and best.spread_pct > abs(delta):
            # Confirmation runs disagree with each other by more than the lead
            # they are supposed to establish. Averaging that into a headline
            # would report a coin flip as a finding.
            verdict = (
                f"**Unstable.** The best candidate (**{config_label}**) averages "
                f"{delta:+.1f}% against production {numbers}, but its "
                f"{best.n} measurements spread {best.spread_pct:.1f}% — wider "
                f"than the improvement itself, so the lead is not established."
            )
        elif delta > 0:
            verdict = (
                f"**{config_label}** beats production by **{delta:+.1f}%** "
                f"{numbers}{evidence}"
            )
        else:
            verdict = (
                f"nothing beat production; best candidate was {delta:+.1f}% {numbers}"
            )
        lines += [verdict_heading, "", verdict, ""]
    elif ranked:
        lines += [
            verdict_heading,
            "",
            f"Best: **{_config_summary(ranked[0].config, varying)}** "
            f"({_fmt(ranked[0].mean)}). No baseline was measured, so there is "
            "nothing to compare against — run a canary next time.",
            "",
        ]
    elif rejected:
        lines += [
            verdict_heading,
            "",
            f"No config stayed inside the objective's redlines. "
            f"{len(rejected)} config(s) completed but breached them — see below.",
            "",
        ]
    else:
        lines += [verdict_heading, "", "No successful runs to rank.", ""]

    # -- leaderboard ---------------------------------------------------------
    if ranked or baseline_runs:
        # The spread column only earns its place once something was repeated.
        header = f"| Config | {target} | vs production | Runs |"
        divider = "|---|---|---|---|"
        if repeated:
            header = f"| Config | {target} | vs production | Measurements | Spread | Runs |"
            divider = "|---|---|---|---|---|---|"
        lines += [results_heading, "", header, divider]
        if baseline_runs:
            padding = " — | — |" if repeated else ""
            lines.append(
                f"| production (as handed over) | {_fmt(baseline_value)} | — |"
                f"{padding} {baseline_runs[0].id} |"
            )
        for group in ranked:
            gain = improvement_pct(group.mean, baseline_value, campaign.objective)
            delta = f"{gain:+.1f}%" if gain is not None else "—"
            summary = _config_summary(group.config, varying)
            run_ids = ", ".join(str(r.id) for r in group.runs)
            if repeated:
                spread = (
                    f"{group.spread_pct:.1f}%" if group.spread_pct is not None else "—"
                )
                lines.append(
                    f"| {summary} | {_fmt(group.mean)} | {delta} | {group.n} | "
                    f"{spread} | {run_ids} |"
                )
            else:
                lines.append(f"| {summary} | {_fmt(group.mean)} | {delta} | {run_ids} |")
        lines.append("")

    # -- rejected by a redline -----------------------------------------------
    if rejected:
        lines += [
            "## Ran, but crossed one of the objective's redlines",
            "",
            f"| Config | {target} | Breached | Runs |",
            "|---|---|---|---|",
        ]
        for group in sorted(rejected, key=lambda g: sort_key(g.mean, campaign.objective)):
            summary = _config_summary(group.config, varying)
            broken = "; ".join(group.breaches)
            # A config that breached on SOME repeats is the most dangerous
            # kind: it would have been crowned on any single one of them.
            if group.n > 1:
                clean = group.n - sum(1 for r in group.runs if crossed_redlines(r))
                if clean:
                    broken += f" (held on {clean} of {group.n} measurements)"
            run_ids = ", ".join(str(r.id) for r in group.runs)
            lines.append(f"| {summary} | {_fmt(group.mean)} | {broken} | {run_ids} |")
        lines.append("")

    # -- failures ------------------------------------------------------------
    if failed:
        lines += ["## Failures", "", "| Config | Why | Run |", "|---|---|---|"]
        for run in failed:
            reason = run.failure_class or run.status
            # `"  ".splitlines()` is [] — guard on the stripped text, not the
            # raw value, or a whitespace-only error crashes the report.
            error_lines = (run.error or "").strip().splitlines()
            detail = error_lines[0][:80] if error_lines else ""
            lines.append(
                f"| {_config_summary(run.candidate.config, varying)} | `{reason}`"
                f"{' — ' + detail if detail else ''} | {run.id} |"
            )
        lines.append("")

    # -- what to do next -----------------------------------------------------
    next_steps: list[str] = []
    classes = {r.failure_class for r in failed if r.failure_class}
    if "oom" in classes:
        next_steps.append(
            "Some configs ran out of memory — lower `mem_fraction_static` or raise `tp`."
        )
    if "ready_timeout" in classes:
        next_steps.append(
            "Some configs never became ready — check model load time against the "
            "readiness timeout."
        )
    if {"image_missing", "ssh_timeout", "gone"} & classes:
        next_steps.append(
            "Some failures were infrastructure, not configuration — those candidates "
            "deserve a retry rather than being written off."
        )
    best_gain = (
        improvement_pct(ranked[0].mean, baseline_value, campaign.objective)
        if ranked
        else None
    )
    if best_gain is not None and best_gain > 0:
        if ranked[0].n < 2:
            # The platform can do this itself now — say so rather than leaving
            # the reader to remember it at 8am.
            next_steps.append(
                "The winner rests on a single benchmark. Set `confirm_top_k` on "
                "the campaign to have the platform re-run the best candidates "
                "and report their spread before crowning one."
            )
        elif verified:
            # Real traffic already had its say; sending the reader off to
            # validate against it again would be advice this campaign followed.
            pass
        else:
            next_steps.append(
                "Validate the winner against current traffic before promoting it. "
                "Repeated runs agree, but they all used the same synthetic "
                "workload — set `verify_benchmark_slug` and `verify_top_k` to have "
                "the platform replay production requests against the best few."
            )
    if not baseline_runs:
        next_steps.append(
            "No baseline canary ran, so these numbers float free of production."
        )
    if next_steps:
        lines += ["## Suggested next steps", ""]
        lines += [f"- {step}" for step in next_steps]
        lines.append("")

    machine_names = sorted({machines[r.machine_id].name for r in runs if r.machine_id in machines})
    if machine_names:
        lines += [f"*Machines: {', '.join(machine_names)}*"]
    return "\n".join(lines)
