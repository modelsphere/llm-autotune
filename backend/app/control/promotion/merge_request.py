"""A LaunchConfig as a change request against its baseline's deploy file.

`build_merge_request` takes a target config — a campaign winner — the bound
baseline whose file production is deployed from, and the file as it is at
branch head, and produces everything the request will carry: the edited file,
the unified diff, a knob-level change table, the evidence, and the things it
deliberately did not touch. It does not write anywhere — the preview endpoint
shows the draft, a promotion target ships it.

What it is careful to surface rather than hide:
- **policy**: only platform-owned knobs and fields are written. Repo-owned
  differences are listed as "differs, not written"; ignored ones as a quiet
  list; kept knobs (in production, absent from the target) are named.
- **staleness**: the baseline was synced at one commit; the branch may have
  moved. The draft is always built against HEAD (an MR against anything else
  conflicts), and lists the knobs that changed in production in between.
- **offline**: when GitLab cannot be read the draft is built on the stored
  copy. Good enough to preview; never good enough to commit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.control.baseline_repo import Document, bound, fetch_document
from app.control.deploy_format import ChangePlan, chart_owned_of, get_format
from app.control.deploy_format.base import unified_diff
from app.control.deploy_format.policy import plan_changes
from app.control.gitlab_client import GitLabClient
from app.control.launch_config import LaunchConfig
from app.core.config import get_settings
from app.db.models import Baseline, DeployBinding


@dataclass
class Origin:
    """Where the target config came from, for the title, the description and
    the link back."""

    kind: str = "campaign"
    campaign_id: int = 0
    campaign_name: str = ""
    run_id: int = 0

    @property
    def page(self) -> str:
        return f"/campaigns/{self.campaign_id}"

    def describe(self) -> str:
        return f"campaign **{self.campaign_name}** (#{self.campaign_id}), run #{self.run_id}"


@dataclass
class MergeRequestDraft:
    ready: bool
    reason: str = ""
    baseline_id: int | None = None
    repo_project: str = ""
    repo_branch: str = ""  # the branch this merge request targets
    tracked_branch: str = ""  # the branch the baseline is synced against
    repo_path: str = ""
    head_commit: str = ""
    synced_commit: str = ""
    stale: bool = False
    offline: bool = False
    # Not-ready because there is genuinely nothing to propose: the winner is
    # the configuration production already runs. A correct outcome, not a
    # failure — the unattended path has to tell the two apart.
    unchanged: bool = False
    unresolved: int = 0
    drift: list[dict[str, Any]] = field(default_factory=list)
    plan: dict[str, Any] = field(default_factory=dict)
    diff: str = ""
    new_text: str = ""
    source_branch: str = ""
    title: str = ""
    description: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "reason": self.reason,
            "baseline_id": self.baseline_id,
            "repo_project": self.repo_project,
            "repo_branch": self.repo_branch,
            "tracked_branch": self.tracked_branch,
            "repo_path": self.repo_path,
            "head_commit": self.head_commit,
            "synced_commit": self.synced_commit,
            "stale": self.stale,
            "offline": self.offline,
            "unchanged": self.unchanged,
            "unresolved": self.unresolved,
            "drift": self.drift,
            "plan": self.plan,
            "diff": self.diff,
            "new_text": self.new_text,
            "source_branch": self.source_branch,
            "title": self.title,
            "description": self.description,
            "evidence": self.evidence,
        }


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "model"


def source_branch_name(
    served_model_name: str,
    run_id: int,
    now: datetime | None = None,
    prefix: str = "autotune/",
) -> str:
    """The branch the change is committed to.

    The prefix is configurable because projects enforce branch-name push rules
    and a rejected push means no merge request at all — the deploy repo allows
    only a fixed set of leading words, so `autotune/` is refused there and
    `feat/autotune-` is not.
    """
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M")
    return f"{prefix}{_slug(served_model_name)}-run{run_id}-{stamp}"


def build_merge_request(
    target: LaunchConfig,
    baseline: Baseline | None,
    origin: Origin,
    *,
    client: GitLabClient | None = None,
    document: Document | None = None,
    branch: str = "",
    apply_removals: bool = False,
    promote_fields: set[str] | frozenset[str] = frozenset(),
    actor: str = "",
    notes: str = "",
    evidence: dict[str, Any] | None = None,
    ui_url: str = "",
    now: datetime | None = None,
) -> MergeRequestDraft:
    """The draft, or a not-ready one saying why (no binding, nothing to change).

    `branch` overrides which release branch the merge request goes onto — the
    deploy repo keeps one per (model x card x engine), so the campaign names
    it and the binding's own branch is only the default.
    `document` overrides the fetch — tests and the target's second pass use it.
    """
    evidence = dict(evidence or {})
    if baseline is None:
        return MergeRequestDraft(
            ready=False,
            evidence=evidence,
            reason=(
                f"no baseline is defined for {target.served_model_name} / {target.engine}"
                + (f" on {target.gpu_type}" if target.gpu_type else "")
                + " — add one on the Baselines page and bind it to the deploy repo"
            ),
        )
    binding = bound(baseline)
    if binding is None:
        return MergeRequestDraft(
            ready=False,
            baseline_id=baseline.id,
            evidence=evidence,
            reason=(
                f"baseline #{baseline.id} ({baseline.served_model_name}) is not bound to a "
                "deploy repo — set its GitLab project, branch and file on the Baselines page"
            ),
        )

    target_branch = (branch or binding.branch).strip()
    tracked = target_branch == binding.branch
    document = document or fetch_document(binding, client, ref=target_branch)
    fmt = get_format(binding.format)
    head = fmt.parse(document.text, target.engine)
    # "Stale" compares the platform's copy with the branch it was copied from;
    # against any other branch the question does not arise — the file there was
    # never synced, so every difference is simply that branch's own config.
    stale = tracked and bool(
        document.commit and binding.commit and document.commit != binding.commit
    )
    moved: list[dict[str, Any]] = []
    if stale and binding.document:
        from app.control.baseline_repo import drift_between

        moved = drift_between(
            fmt.parse(binding.document, target.engine).engine_args, head.engine_args
        )

    plan = plan_changes(
        head,
        target,
        policy=binding.policy or {},
        equivalences=binding.equivalences or {},
        chart_owned=chart_owned_of(fmt),
        apply_removals=apply_removals,
        promote_fields=frozenset(promote_fields),
        engine=target.engine,
    )
    if head.warnings:
        plan.warnings = list(head.warnings) + plan.warnings
    if not tracked:
        plan.warnings.insert(
            0,
            f"this merge request targets `{target_branch}`, but the baseline is synced against "
            f"`{binding.branch}` — the ownership decisions and equivalences come from that "
            "branch, and nothing here has been reconciled with this one",
        )

    draft = MergeRequestDraft(
        ready=not plan.empty,
        baseline_id=baseline.id,
        repo_project=binding.project,
        repo_branch=target_branch,
        tracked_branch=binding.branch,
        repo_path=binding.path,
        head_commit=document.commit,
        synced_commit=binding.commit,
        stale=stale,
        offline=document.offline,
        unresolved=binding.unresolved,
        drift=moved,
        plan=plan.payload(),
        evidence=evidence,
    )
    if plan.empty:
        draft.unchanged = True
        draft.reason = "production already runs this configuration — nothing to change"
        return draft

    draft.new_text = fmt.apply(document.text, head, plan)
    draft.diff = unified_diff(document.text, draft.new_text, binding.path)
    draft.source_branch = source_branch_name(
        target.served_model_name, origin.run_id, now, get_settings().gitlab_branch_prefix
    )
    draft.title = _title(target, origin, plan)
    draft.description = _describe(
        target, baseline, binding, origin, plan, draft, actor=actor, notes=notes, ui_url=ui_url
    )
    return draft


# ---------------------------------------------------------------------------
# text
# ---------------------------------------------------------------------------


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if value is True:
        return "on"
    if value is False:
        return "off"
    return str(value)


def _title(target: LaunchConfig, origin: Origin, plan: ChangePlan) -> str:
    knobs = [c.flag or c.key for c in plan.changes]
    what = ", ".join(knobs[:3]) + (f" +{len(knobs) - 3}" if len(knobs) > 3 else "")
    if not what and plan.gpus_after is not None:
        what = "gpus"
    if not what and plan.image_tag_after:
        what = "image"
    if not what and plan.model_path_after:
        what = "model path"
    return f"autotune: {target.served_model_name} tuned config — {what} (run {origin.run_id})"


def _describe(
    target: LaunchConfig,
    baseline: Baseline,
    binding: DeployBinding,
    origin: Origin,
    plan: ChangePlan,
    draft: MergeRequestDraft,
    *,
    actor: str,
    notes: str,
    ui_url: str,
) -> str:
    evidence = draft.evidence
    lines: list[str] = [
        f"## Tuned {target.engine} configuration for `{target.served_model_name}`"
        + (f" on {baseline.card_type}" if baseline.card_type else ""),
        "",
        f"Generated by the LLM autotune platform from {origin.describe()}. Only the knobs "
        f"below change in `{binding.path}`; everything else in the file is as on "
        f"`{draft.repo_branch}`.",
        "",
        "### Changes",
        "",
        "| knob | production | tuned |",
        "|---|---|---|",
    ]
    for change in plan.changes:
        lines.append(
            f"| `{change.flag or change.key}` | {_fmt(change.before)} | {_fmt(change.after)} |"
        )
    if plan.gpus_after is not None:
        lines.append(f"| gpus per replica | {plan.gpus_before} | {plan.gpus_after} |")
    if plan.image_tag_after:
        lines.append(f"| image tag | {plan.image_tag_before} | {plan.image_tag_after} |")
    if plan.model_path_after:
        lines.append(f"| model path | {plan.model_path_before} | {plan.model_path_after} |")

    lines += ["", "### Evidence", ""]
    metric = evidence.get("target_metric") or ""
    if metric:
        vs = evidence.get("vs_baseline")
        vs_text = (
            f" — ×{vs:.3f} of the production baseline on the same benchmark"
            if isinstance(vs, int | float)
            else ""
        )
        lines.append(f"- **{metric}**: {evidence.get('score')}{vs_text}")
    if evidence.get("benchmark_slug"):
        stage = f" ({evidence['stage']} stage)" if evidence.get("stage") else ""
        lines.append(f"- benchmark: `{evidence['benchmark_slug']}`{stage}")
    if evidence.get("dataset_build_id"):
        lines.append(f"- dataset build: `{evidence['dataset_build_id']}`")
    if "feasible" in evidence:
        if evidence.get("feasible"):
            lines.append("- redlines: held")
        else:
            lines.append(f"- redlines: **crossed** — {'; '.join(evidence.get('breaches') or [])}")
    if evidence.get("image_ref"):
        digest = evidence.get("image_digest")
        lines.append(
            f"- measured on image `{evidence['image_ref']}`"
            + (f" (digest `{digest}`)" if digest and digest not in evidence["image_ref"] else "")
        )
    if evidence.get("card_type") or baseline.card_type:
        lines.append(f"- card: {evidence.get('card_type') or baseline.card_type}")
    if evidence.get("engine_version"):
        lines.append(f"- engine version: {evidence['engine_version']}")

    caveats: list[str] = []
    if draft.stale:
        head, synced = draft.head_commit[:10], draft.synced_commit[:10]
        caveats.append(
            f"the platform's baseline was synced from `{synced}`; the branch is now at `{head}`"
            + (
                " and these knobs changed in production in between: "
                + ", ".join(
                    f"`{d['key']}` {_fmt(d['before'])} → {_fmt(d['after'])}" for d in draft.drift
                )
                if draft.drift
                else " (no engine-arg change in between)"
            )
        )
    if draft.unresolved:
        caveats.append(
            f"{draft.unresolved} difference(s) between the platform's baseline and this file are "
            "still unresolved on the Baselines page"
        )
    for kept in plan.kept:
        caveats.append(
            f"`{kept.flag or kept.key}={_fmt(kept.before)}` is in production but not in the tuned "
            "config — kept as is (the search space never set it)"
        )
    for rep in plan.reported:
        caveats.append(
            f"`{rep.flag or rep.key}` differs — production {_fmt(rep.before)}, measured with "
            f"{_fmt(rep.after)} — not written ({rep.note})"
        )
    caveats += plan.warnings
    if caveats:
        lines += ["", "### Not applied / check by hand", ""]
        lines += [f"- {c}" for c in caveats]
    if plan.ignored:
        lines += ["", "<details><summary>Ignored by policy</summary>", ""]
        lines += [
            f"- `{i.flag or i.key}`: production {_fmt(i.before)}, measured with {_fmt(i.after)}"
            for i in plan.ignored
        ]
        lines += ["", "</details>"]

    lines += ["", "### Diff", "", "```diff", draft.diff.rstrip("\n"), "```"]
    if notes:
        lines += ["", notes]
    footer = []
    if actor:
        footer.append(f"requested by {actor}")
    if ui_url:
        footer.append(f"platform page: {ui_url.rstrip('/')}{origin.page}")
    if footer:
        lines += ["", "_" + " · ".join(footer) + "_"]
    return "\n".join(lines)
