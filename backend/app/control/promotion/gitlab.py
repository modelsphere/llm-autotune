"""GitLab promotion target — open a merge request that changes production's
deploy file to the winner's configuration.

The target ships a *draft* the API prepared with build_merge_request (see
merge_request.py): the edited `config/model.yaml`, the diff, the title and
description. It never decides what to change — that was the search's job,
under the baseline's ownership policy — it only:

  1. creates a branch off the baseline's release branch with one commit that
     updates the bound file (Commits API),
  2. opens a merge request from it targeting that release branch,
  3. optionally triggers a pipeline on the branch.

`status()` maps the MR state back onto PromotionState: open → SUBMITTED,
merged → ROLLED_OUT (the deploy repo's own pipeline then offers the manual
production deploy), closed unmerged → REJECTED. The A/B-test graduation is
still a documented hook.

Dry-run is the default (`AUTOTUNE_PROMOTION_DRY_RUN=true`): the draft is
recorded — branch name, diff, title — and nothing is written to GitLab, so
the whole path is exercisable before the token is trusted with a real
project. A draft built OFFLINE (from the stored copy of the file, GitLab
unreachable) is never committed even when armed: the branch may have moved.
"""

from __future__ import annotations

import logging

from app.control.gitlab_client import GitLabClient, GitLabError, GitLabUnavailable
from app.control.promotion.base import (
    PromotionError,
    PromotionHandle,
    PromotionRequest,
    PromotionState,
    PromotionStatus,
    PromotionTarget,
    PromotionUnavailable,
)
from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)


class GitLabPromotionTarget(PromotionTarget):
    name = "gitlab"

    def __init__(self, settings: Settings | None = None, client: GitLabClient | None = None):
        self.settings = settings or get_settings()
        self.dry_run = self.settings.promotion_dry_run
        self.client = client or GitLabClient(self.settings)
        if not self.dry_run and not self.client.configured:
            raise PromotionUnavailable(
                "gitlab promotion target is not configured: set AUTOTUNE_GITLAB_BASE_URL and "
                "AUTOTUNE_GITLAB_TOKEN (or leave AUTOTUNE_PROMOTION_DRY_RUN=true)"
            )

    # -- PromotionTarget -----------------------------------------------------

    def open_rollout(self, request: PromotionRequest) -> PromotionHandle:
        draft = request.draft or {}
        if not draft:
            raise PromotionUnavailable(
                "the gitlab target needs a merge-request draft: the baseline must be bound to "
                "its deploy repo (Baselines page) before a winner can be promoted this way"
            )
        if not draft.get("ready"):
            raise PromotionError(draft.get("reason") or "nothing to change")
        project = draft["repo_project"]
        refs = {
            "project": project,
            "branch": draft["source_branch"],
            "target_branch": draft["repo_branch"],
            "path": draft["repo_path"],
            "head_commit": draft.get("head_commit", ""),
            "mr_title": draft["title"],
            "diff": draft.get("diff", ""),
            "plan": draft.get("plan") or {},
        }
        if self.dry_run:
            logger.info(
                "[dry-run] would commit %s on %s (from %s) and open MR %r in %s",
                refs["path"],
                refs["branch"],
                refs["target_branch"],
                refs["mr_title"],
                project,
            )
            return PromotionHandle(
                target=self.name,
                state=PromotionState.SUBMITTED,
                refs={**refs, "dry_run": True, "description": draft.get("description", "")},
                detail="dry-run: no merge request was created "
                "(set AUTOTUNE_PROMOTION_DRY_RUN=false to arm)",
            )
        if draft.get("offline"):
            raise PromotionError(
                "the draft was built from the stored copy of the file (GitLab was unreachable); "
                "refresh the preview with GitLab reachable before opening the merge request"
            )
        try:
            self.client.commit_file(
                project,
                branch=refs["branch"],
                start_branch=refs["target_branch"],
                path=refs["path"],
                content=draft["new_text"],
                message=draft["title"],
            )
            mr = self.client.create_merge_request(
                project,
                source_branch=refs["branch"],
                target_branch=refs["target_branch"],
                title=draft["title"],
                description=draft.get("description", ""),
                labels=["autotune"],
            )
        except GitLabUnavailable as exc:
            raise PromotionUnavailable(str(exc)) from exc
        except GitLabError as exc:
            raise PromotionError(str(exc)) from exc
        refs.update({"mr_iid": mr.get("iid"), "mr_url": mr.get("web_url")})
        if self.settings.gitlab_trigger_pipeline:
            try:
                pipeline = self.client.trigger_pipeline(project, refs["branch"])
                refs["pipeline_id"] = pipeline.get("id")
                refs["pipeline_url"] = pipeline.get("web_url")
            except GitLabError as exc:  # the MR exists; a pipeline is a nicety
                refs["pipeline_error"] = str(exc)
        return PromotionHandle(
            target=self.name,
            state=PromotionState.SUBMITTED,
            refs=refs,
            detail=f"merge request {mr.get('web_url', '')} opened",
        )

    def status(self, handle: PromotionHandle) -> PromotionStatus:
        if handle.refs.get("dry_run") or self.dry_run:
            return PromotionStatus(state=handle.state, detail=handle.detail, refs=handle.refs)
        iid, project = handle.refs.get("mr_iid"), handle.refs.get("project")
        if not iid or not project:
            return PromotionStatus(state=handle.state, detail=handle.detail, refs=handle.refs)
        try:
            mr = self.client.get_merge_request(project, iid)
        except GitLabUnavailable as exc:
            raise PromotionUnavailable(str(exc)) from exc
        except GitLabError as exc:
            raise PromotionError(str(exc)) from exc
        mr_state = mr.get("state")  # opened | merged | closed | locked
        refs = {
            **handle.refs,
            "mr_state": mr_state,
            "merge_commit_sha": mr.get("merge_commit_sha") or "",
        }
        if mr_state == "closed":
            return PromotionStatus(
                state=PromotionState.REJECTED, detail="merge request was closed unmerged", refs=refs
            )
        if mr_state == "merged":
            return PromotionStatus(
                state=PromotionState.ROLLED_OUT,
                detail="merged — the deploy repo's branch pipeline now offers the manual "
                "production deploy; re-sync the baseline to mirror the new head "
                "(A/B graduation is TBD)",
                refs=refs,
            )
        return PromotionStatus(
            state=PromotionState.SUBMITTED, detail="merge request open", refs=refs
        )

    def cancel(self, handle: PromotionHandle) -> None:
        if self.dry_run or handle.refs.get("dry_run"):
            return
        iid, project = handle.refs.get("mr_iid"), handle.refs.get("project")
        if iid and project:
            try:
                self.client.close_merge_request(project, iid)
            except GitLabError as exc:
                raise PromotionError(str(exc)) from exc
