"""Keeping a bound baseline in step with its deploy-repo file.

A baseline with a DeployBinding mirrors one file on one branch. `sync` reads
that file at branch head through the binding's format adapter, then — under
the binding's ownership policy — adopts the platform-owned differences into
the row, records the rest as divergences for a person to resolve, and stores
the commit and the text. Nothing is overwritten that the policy does not say
the platform owns, and every difference it does not adopt is on the record.

GitLab may be unconfigured (a dev box) or unreachable (behind a VPN). The
document stored at the last sync is then the fallback, flagged `offline`, so
a merge request can still be PREVIEWED against the last known production
config; only opening one needs the live host.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.control.deploy_format import ParsedFile, chart_owned_of, get_format
from app.control.deploy_format.policy import (
    add_equivalence,
    compare,
    merge_divergences,
    set_policy,
)
from app.control.gitlab_client import GitLabClient, GitLabUnavailable
from app.control.launch_config import LaunchConfig
from app.db.models import Baseline, DeployBinding


@dataclass
class Document:
    text: str
    commit: str
    offline: bool = False  # served from the stored copy, not from GitLab
    error: str = ""  # why, when offline
    branch: str = ""  # which branch it was read from ("" = the tracked one)


@dataclass
class SyncReport:
    document: Document
    parsed: ParsedFile
    theirs: LaunchConfig
    adopted: list[dict[str, Any]] = field(default_factory=list)
    divergences: list[dict[str, Any]] = field(default_factory=list)

    @property
    def unresolved(self) -> int:
        return sum(1 for d in self.divergences if d.get("status") == "unresolved")


def bound(baseline: Baseline) -> DeployBinding | None:
    binding = baseline.binding
    if binding is None or not (binding.project and binding.branch and binding.path):
        return None
    return binding


def fetch_document(
    binding: DeployBinding, client: GitLabClient | None = None, ref: str = ""
) -> Document:
    """The bound file at head of `ref` — by default the branch the binding
    tracks — or the stored copy when GitLab cannot be asked.

    The stored copy stands only for the TRACKED branch: it is the text of the
    last sync, and the deploy repo's other release branches hold a different
    model on different cards. Asking for one of those offline is an error, not
    a fallback: answering with the tracked branch's file would diff a config
    against the wrong production.
    """
    branch = ref or binding.branch
    tracked = branch == binding.branch
    client = client or GitLabClient()
    if not client.configured:
        if binding.document and tracked:
            return Document(
                binding.document,
                binding.commit,
                offline=True,
                error="GitLab is not configured; using the copy from the last sync",
                branch=branch,
            )
        raise GitLabUnavailable(
            "GitLab is not configured and this binding has never been synced"
            if tracked
            else f"GitLab is not configured, so `{branch}` cannot be read — the platform only "
            f"has a copy of `{binding.branch}`"
        )
    try:
        got = client.get_file(binding.project, binding.path, branch)
    except GitLabUnavailable as exc:
        if binding.document and tracked:
            return Document(
                binding.document, binding.commit, offline=True, error=str(exc), branch=branch
            )
        raise
    return Document(got["content"], got["commit"], branch=branch)


def read(binding: DeployBinding, text: str, engine: str) -> tuple[ParsedFile, LaunchConfig]:
    fmt = get_format(binding.format)
    parsed = fmt.parse(text, engine)
    theirs = LaunchConfig.from_parsed(parsed.as_launch_dict(engine), engine)
    return parsed, theirs


def sync(
    baseline: Baseline,
    binding: DeployBinding,
    *,
    client: GitLabClient | None = None,
    document: Document | None = None,
    now: datetime | None = None,
) -> SyncReport:
    """Re-read the file and reconcile the row with it under the policy.
    Mutates the row and the binding; the caller commits. A sync reads the
    LIVE file — it never silently falls back to the stored copy."""
    if document is None:
        client = client or GitLabClient()
        client.require()
        got = client.get_file(binding.project, binding.path, binding.branch)
        document = Document(got["content"], got["commit"])
    fmt = get_format(binding.format)
    parsed = fmt.parse(document.text, baseline.engine)
    theirs = LaunchConfig.from_parsed(parsed.as_launch_dict(baseline.engine), baseline.engine)
    ours = LaunchConfig.from_baseline(baseline)

    adopt, divergences = compare(
        ours,
        theirs,
        parsed,
        binding.policy or {},
        binding.equivalences or {},
        chart_owned=chart_owned_of(fmt),
    )
    _adopt(baseline, adopt)
    binding.divergences = merge_divergences(binding.divergences or [], divergences)
    binding.commit = document.commit
    binding.document = document.text
    binding.synced_at = now or datetime.now(UTC)
    baseline.source = f"gitlab:{binding.branch}@{document.commit[:10]}"
    return SyncReport(
        document=document,
        parsed=parsed,
        theirs=theirs,
        adopted=adopt,
        divergences=list(binding.divergences),
    )


def import_config(
    baseline: Baseline, binding: DeployBinding, document: Document, *, now: datetime | None = None
) -> SyncReport:
    """First sync of a row that has no config of its own yet: the file IS the
    config, every field adopted, nothing to reconcile."""
    fmt = get_format(binding.format)
    parsed = fmt.parse(document.text, baseline.engine)
    theirs = LaunchConfig.from_parsed(parsed.as_launch_dict(baseline.engine), baseline.engine)
    theirs.apply_to_baseline(baseline)
    binding.divergences = []
    binding.commit = document.commit
    binding.document = document.text
    binding.synced_at = now or datetime.now(UTC)
    baseline.source = f"gitlab:{binding.branch}@{document.commit[:10]}"
    return SyncReport(document=document, parsed=parsed, theirs=theirs)


def _adopt(baseline: Baseline, rows: list[dict[str, Any]]) -> None:
    args = dict(baseline.engine_args or {})
    for row in rows:
        if row["kind"] == "knob":
            if row["theirs"] is None:
                args.pop(row["key"], None)
            else:
                args[row["key"]] = row["theirs"]
        elif row["key"] in ("image", "model_path"):
            setattr(baseline, row["key"], row["theirs"] or "")
        elif row["key"] == "extra_env":
            baseline.extra_env = dict(row["theirs"] or {})
        # gpus is derived from the knobs on our side; nothing to write.
    baseline.engine_args = args


# ---------------------------------------------------------------------------
# resolving a divergence
# ---------------------------------------------------------------------------

RESOLUTIONS = ("adopt", "equivalent", "repo", "ignore", "platform")


def resolve(
    baseline: Baseline,
    binding: DeployBinding,
    *,
    kind: str,
    key: str,
    action: str,
    actor: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply one decision to one divergence and record it.

    adopt       take the repo's value into the row (owner stays as is)
    equivalent  declare ours ↔ theirs the same thing
    repo        the repo keeps its value; differences stay reported
    ignore      the repo keeps its value; differences are not reported
    platform    the platform owns it: adopted now, proposed in merge requests
    """
    if action not in RESOLUTIONS:
        raise ValueError(f"action must be one of {RESOLUTIONS}")
    entry = next(
        (d for d in (binding.divergences or []) if d.get("kind") == kind and d.get("key") == key),
        None,
    )
    if entry is None:
        raise KeyError(f"no divergence for {kind} {key}")
    ours, theirs = entry.get("ours"), entry.get("theirs")
    policy = dict(binding.policy or {})
    equivalences = dict(binding.equivalences or {})

    if action == "equivalent":
        if kind == "knob":
            equivalences = add_equivalence(equivalences, knob=key, ours=ours, theirs=theirs)
        else:
            equivalences = add_equivalence(equivalences, field=key, ours=ours, theirs=theirs)
    elif action in ("repo", "ignore", "platform"):
        policy = set_policy(
            policy, **({"knob": key} if kind == "knob" else {"field": key}), owner=action
        )
    if action in ("adopt", "platform"):
        _adopt(baseline, [entry])

    stamped = {
        **entry,
        "status": action,
        "decided_by": actor,
        "decided_at": (now or datetime.now(UTC)).isoformat(),
    }
    binding.divergences = [
        stamped if (d.get("kind") == kind and d.get("key") == key) else d
        for d in (binding.divergences or [])
    ]
    binding.policy = policy
    binding.equivalences = equivalences
    return stamped


def drift_between(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """Knob-level difference between two engine configs, as rows — what moved
    in production between two commits."""
    from app.control.deploy_format.policy import same_value

    rows: list[dict[str, Any]] = []
    for key in sorted(set(before) | set(after)):
        if key in before and key in after:
            if not same_value(before[key], after[key]):
                rows.append(
                    {"key": key, "kind": "changed", "before": before[key], "after": after[key]}
                )
        elif key in after:
            rows.append({"key": key, "kind": "added", "before": None, "after": after[key]})
        else:
            rows.append({"key": key, "kind": "removed", "before": before[key], "after": None})
    return rows
