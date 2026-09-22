"""Promotion interface — the CICD compatibility seam.

A campaign's job ends with a *winner*: one configuration, measured, that beats
production. Promotion is what happens next — handing that exact config to
whatever rolls it onto the serving cluster. Today that is "a human copies the
command"; the fleet is moving to a GitLab-based CICD plus an A/B-test system
that will take a config, stage it behind a traffic split, and graduate it. The
details are unconfirmed, so this is a seam, not an integration: a `PromotionTarget`
renders a winner into whatever its substrate needs and reports the rollout's
state back, exactly as a `DeploymentDriver` does for a run.

The contract mirrors the driver's on purpose:
- open_rollout() submits and returns fast with a handle (refs to the external
  artifacts it created — a merge request, a pipeline, an A/B experiment).
- status() is polled; it maps the external system's state onto PromotionState.
- the handle is derived from stored refs, so a worker restart can resume
  polling a rollout it did not start.

What flows across the seam is a `PromotionConfig` (see config.py): the winner's
*exact, reproducible* engine configuration — not a leaderboard row. Selecting
the winner is the leaderboard's job; this layer only ships what it is handed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class PromotionState(StrEnum):
    """Where a rollout sits, normalized across whatever external system runs it.

        DRAFT       recorded, not yet handed to CICD
        SUBMITTED   handed over — MR opened / pipeline triggered / artifact ready
        AB_TESTING  live behind a traffic split, being compared to production
        ROLLED_OUT  fully deployed (terminal success)
        REJECTED    turned down by review or the A/B test (terminal)
        FAILED      the hand-off or pipeline errored (terminal)
        CANCELLED   withdrawn by a human (terminal)

    SUBMITTED and AB_TESTING are both "in flight" and deliberately distinct: one
    is waiting on a merge/pipeline, the other on a live comparison whose verdict
    is data, not a click.
    """

    DRAFT = "draft"
    SUBMITTED = "submitted"
    AB_TESTING = "ab_testing"
    ROLLED_OUT = "rolled_out"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_PROMOTION_STATES = {
    PromotionState.ROLLED_OUT,
    PromotionState.REJECTED,
    PromotionState.FAILED,
    PromotionState.CANCELLED,
}


class PromotionError(RuntimeError):
    """A promotion target failed to act. Raised so the API surfaces a specific
    reason rather than a generic 500."""


class PromotionUnavailable(PromotionError):
    """The chosen target is not configured (no GitLab URL/token yet). Distinct
    from a transient failure: it never succeeds until someone configures it."""


class PromotionRequest(BaseModel):
    """Everything a target needs to open one rollout."""

    # The winner's exact config — the payload built by config.py. Opaque to the
    # target beyond the few fields it renders (engine, served model, command).
    config: dict[str, Any]
    campaign_id: int
    campaign_name: str = ""
    run_id: int = 0
    # Who asked, for the commit author / MR description / audit trail.
    actor: str = ""
    notes: str = ""
    # For targets that edit a deploy file: the prepared merge-request draft
    # (merge_request.MergeRequestDraft.payload()) — the edited file, diff,
    # title, description, and where it goes. Built by the API under the
    # baseline's ownership policy; the target ships it as is.
    draft: dict[str, Any] | None = None


class PromotionHandle(BaseModel):
    """Everything needed to find a rollout again after a restart. Derived from
    stored refs, never from memory — same discipline as DeploymentHandle."""

    target: str
    state: PromotionState = PromotionState.DRAFT
    # External identifiers the target created and can poll: merge-request URL,
    # pipeline id, branch, A/B experiment id, or a rendered artifact for the
    # manual path. Shape is target-specific on purpose.
    refs: dict[str, Any] = Field(default_factory=dict)
    detail: str = ""


class PromotionStatus(BaseModel):
    """A poll result: the current state plus any refs that have since appeared
    (a pipeline id that did not exist when the MR was first opened)."""

    state: PromotionState
    detail: str = ""
    refs: dict[str, Any] = Field(default_factory=dict)


class PromotionTarget(ABC):
    name: str = "base"

    @abstractmethod
    def open_rollout(self, request: PromotionRequest) -> PromotionHandle:
        """Submit the winner to this substrate. Returns fast with a handle whose
        refs point at whatever was created. Raises PromotionError on failure."""

    def status(self, handle: PromotionHandle) -> PromotionStatus:
        """Poll the rollout. Default: unchanged — a target with no queryable
        state (the manual export) stays where open_rollout left it."""
        return PromotionStatus(state=handle.state, detail=handle.detail, refs=handle.refs)

    def cancel(self, handle: PromotionHandle) -> None:
        """Withdraw an in-flight rollout, if the substrate supports it. Default:
        nothing to do (the caller still records it CANCELLED)."""
        return None
