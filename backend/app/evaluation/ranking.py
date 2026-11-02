"""Who is winning — the one place that decides it.

The leaderboard endpoint answers it for a person, and the auto-promotion pass
answers it for a merge request. Those must not be two derivations: "the top of
the board" is what an operator reads before clicking Generate MR, and a
supervisor that ranked even slightly differently would open a merge request
for a configuration nobody saw win.

So the ordering lives here, taking plain ORM rows, and both callers bring their
own session — the API an async one, the worker a sync one. Nothing in this
module touches a session or a clock.

The ordering, outermost key first:

1. **verified before screened** — the expensive stage is the better evidence,
   and the two are not one scale (different benchmarks entirely);
2. **comparable before not** — a run measured against a different dataset build
   answers a different question, whatever it scored;
3. **on-card before off-card** — `output_tpm_card_norm` normalizes by card
   COUNT, not card TYPE, so an H100 run is not on an A100 baseline's scale;
4. **inside the redlines before outside** — a config that wins the target while
   crossing an SLO is a rejected option, not a winner;
5. **by the objective**, on the baseline-relative ratio where a same-chip
   baseline exists (drift-robust, and the same ordering when the dataset holds
   still) and on the raw score otherwise.
"""

from __future__ import annotations

from typing import Any

from app.control.search.space import prune_inactive
from app.datasets import pinning
from app.db.models import is_baseline_candidate, is_baseline_config
from app.evaluation.aggregate import summary_of
from app.hardware import normalize_gpu_type
from app.objective import sort_key, target_metric
from app.schemas.core import LeaderboardEntry
from app.staging import VERIFY, stage_of
from app.staging import objective as stage_objective


def _score(result, summary) -> float | None:
    if summary.objective_value is not None:
        return summary.objective_value
    return result.score if isinstance(result.score, int | float) else None


def card_type_of(run) -> str:
    """The card this run landed on, canonicalized. The actual node type wins;
    the machine's declared type is the fallback. Normalized because an older
    run's snapshot may hold a legacy string ("A100-SXM4-80GB")."""
    snap = run.env_snapshot or {}
    return normalize_gpu_type(snap.get("card_type") or snap.get("machine_gpu_type") or "")


def leaderboard_entries(campaign, rows: list[tuple[Any, Any, Any]]) -> list[LeaderboardEntry]:
    """Rank (run, candidate, result) triples for one campaign.

    `rows` is every succeeded run of the campaign that carries an llmbench
    result — the caller's query, because that part differs between an async
    endpoint and the worker's sync session.
    """
    # The production baseline this campaign measured, per stage: the MEAN of
    # every measurement of it, so three nights that each measured production
    # rank against one reference rather than whichever came last. It rode the
    # same dataset as the candidates it is compared to, so a candidate expressed
    # as a multiple of it is comparable across nights even as the dataset (and
    # the absolute numbers) drift. A candidate is only normalized against
    # measurements taken on the SAME chip: the reference card is the one most of
    # the stage's baseline runs used, and only those enter the mean.
    production = _production_configs(campaign, rows)

    def _is_baseline(candidate) -> bool:
        # A policy's search measures production too — its anchor runs carry
        # production's exact config but no baseline flag. They are measurements
        # of the reference all the same. Only in the search: at validation the
        # platform flags its own production run, and a finalist that happens to
        # be production's config stays a finalist (history reads its ratio as
        # the validation's noise).
        if is_baseline_candidate(candidate):
            return True
        return stage_of(candidate) != VERIFY and dict(candidate.config or {}) in production

    measured: dict[str, list[tuple[float, str]]] = {}
    for _run, candidate, result in rows:
        if not _is_baseline(candidate):
            continue
        stage = stage_of(candidate)
        value = _score(result, summary_of(result, stage_objective(campaign, stage)))
        if value:  # a zero or missing reference cannot normalize anything
            measured.setdefault(stage, []).append((value, card_type_of(_run)))
    baseline_by_stage: dict[str, float] = {}
    baseline_card_by_stage: dict[str, str] = {}
    for stage, values in measured.items():
        cards = [card for _value, card in values]
        card = max(dict.fromkeys(cards), key=cards.count)
        same = [value for value, c in values if c == card]
        baseline_by_stage[stage] = sum(same) / len(same)
        baseline_card_by_stage[stage] = card

    entries: list[LeaderboardEntry] = []
    for run, candidate, result in rows:
        stage = stage_of(candidate)
        objective = stage_objective(campaign, stage)
        # Resolved when the result landed; recomputed only for rows that
        # predate that. Either way it is the same function the report ranks
        # on, so the two views cannot disagree about who won.
        summary = summary_of(result, objective)
        replayed = pinning.dataset_of(result.metrics or {})
        score = _score(result, summary)
        card_type = card_type_of(run)
        # Off-chip: this run ran on a different card than the baseline for its
        # stage, so its per-card number is not on the baseline's scale. Suppress
        # the ratio and let it sink rather than presenting an H100 candidate as
        # "1.8x production" when production was measured on A100.
        ref_card = baseline_card_by_stage.get(stage, "")
        off_card = bool(card_type and ref_card and card_type != ref_card)
        reference = None if off_card else baseline_by_stage.get(stage)
        entries.append(
            LeaderboardEntry(
                run_id=run.id,
                config=candidate.config,
                score=score,
                metrics=result.metrics,
                holds_redlines=not summary.breaches,
                breaches=summary.breaches,
                stage=stage,
                target_metric=target_metric(objective),
                comparable=pinning.comparable(campaign, result.metrics or {}),
                dataset_build_id=replayed[0] if replayed else "",
                vs_baseline=(score / reference) if (reference and score is not None) else None,
                is_baseline=_is_baseline(candidate),
                card_type=card_type,
            )
        )

    def _rank_value(entry):
        if entry.vs_baseline is not None:
            return entry.vs_baseline
        return entry.score

    def _off_card(entry) -> bool:
        ref = baseline_card_by_stage.get(entry.stage, "")
        return bool(entry.card_type and ref and entry.card_type != ref)

    entries.sort(
        key=lambda e: (
            e.stage != VERIFY,
            not e.comparable,
            _off_card(e),
            not e.holds_redlines,
            *sort_key(_rank_value(e), stage_objective(campaign, e.stage)),
        )
    )
    return entries


def _production_configs(campaign, rows: list[tuple[Any, Any, Any]]) -> list[dict]:
    """Production's config, as the campaign's candidates spell it: whatever its
    flagged baseline candidates carry. A policy campaign that has not validated
    yet has none, and its production is the campaign's base (prune_inactive
    gives it the canonical form a policy's configs take).

    A recipe campaign's production is the recipe as written: no changes, the
    base alone, which is often the empty config."""
    recipe = bool(getattr(campaign, "recipe", None))
    configs = [
        dict(candidate.config or {})
        for _run, candidate, _result in rows
        if is_baseline_candidate(candidate) and (candidate.config or recipe)
        and not is_baseline_config(candidate.config)
    ]
    if not configs and getattr(campaign, "policy_id", None) is not None:
        space = campaign.search_space or {}
        if space.get("base") or recipe:
            configs.append(prune_inactive(dict(space.get("base") or {}), space))
    return configs


def winner(entries: list[LeaderboardEntry]) -> LeaderboardEntry | None:
    """The top of the board, skipping the production baseline row: it is the
    reference the candidates are measured against, not a candidate."""
    for entry in entries:
        if entry.is_baseline:
            continue
        return entry
    return None
