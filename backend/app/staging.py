"""Two stages of evidence: screen everything cheaply, verify the best properly.

A guidellm sweep takes minutes and answers "is this config in the right
neighbourhood". A production replay takes the better part of an hour and
answers the question we actually care about — how the config behaves on real
traffic, with real prompt lengths and real cache reuse. Running the second one
on every point of a grid would spend a night on four candidates.

So a campaign can carry two benchmarks. Every candidate is screened; the best
`verify_top_k` are then re-run against the expensive one, and that measurement
is what decides the winner.

The two stages report DIFFERENT METRIC NAMES — guidellm's
`perf_guidellm_sweep.c1.output_tps` has no counterpart in replay's flat
`replay.output_tpm`, and vice versa. So a stage carries its own
objective as well as its own benchmark: scoring a replay result against the
screening objective yields None, which reads downstream as "this run produced
nothing" rather than "this run was measured differently".

Everything that needs to know which benchmark a run submits to, or which
objective its result is judged by, asks here. Two derivations of that in two
places is how a run gets submitted to one benchmark and scored against the
other's metric names.
"""

from typing import Any

from app.db.models import Campaign, Candidate, CandidateKind, Run
from app.metrics_catalog import DEFAULT_VERIFY_TARGET_METRIC

SCREEN = "screen"
VERIFY = "verify"

# What the expensive stage is worth waiting for, when a campaign turns it on
# without saying how long. A full replay of the rolling 1000-request dataset
# runs 20-60 minutes on 8 cards; the benchmark's own hard cap is 5 hours.
DEFAULT_VERIFY_MAX_RUN_MINUTES = 180


def is_staged(campaign: Campaign) -> bool:
    """Does this campaign have a second, expensive stage at all?

    Both halves are required: a slug with no top-k would verify nothing, and a
    top-k with no slug would re-run the cheap benchmark and call it proof.
    """
    return bool(campaign.verify_benchmark_slug) and campaign.verify_top_k > 0


def stage_of(candidate: Candidate | None) -> str:
    """Which stage a candidate — and so the run carrying it — belongs to."""
    if candidate is not None and candidate.kind == CandidateKind.VERIFICATION.value:
        return VERIFY
    return SCREEN


def stage_of_run(run: Run) -> str:
    return stage_of(getattr(run, "candidate", None))


def benchmark_slug(campaign: Campaign, stage: str) -> str:
    """The LLMBench benchmark this stage submits to. "" = platform default."""
    if stage == VERIFY:
        return campaign.verify_benchmark_slug or ""
    return campaign.benchmark_slug or ""


def objective(campaign: Campaign, stage: str) -> dict[str, Any]:
    """The objective a stage's results are judged by.

    A campaign that turns on verification without configuring its objective
    gets the platform default for the expensive stage — which is a replay
    metric, not the screening one. Falling back to `campaign.objective` here
    would score every replay result None and quietly finish the campaign with
    "nothing stayed inside the redlines".
    """
    if stage == VERIFY:
        return campaign.verify_objective or {"target_metric": DEFAULT_VERIFY_TARGET_METRIC}
    return campaign.objective or {}


def objective_of_run(run: Run) -> dict[str, Any]:
    return objective(run.campaign, stage_of_run(run))


def max_run_minutes(campaign: Campaign, stage: str, default: int) -> int:
    """How long to reserve before the window closes for one run of this stage.

    The expensive stage gets its own number: reserving the screening run's 150
    minutes for a replay is how a window closes on top of a benchmark that had
    twenty minutes left to go, killing it for nothing.
    """
    if stage == VERIFY:
        return (
            campaign.verify_max_run_minutes
            or campaign.max_run_minutes
            or default
        )
    return campaign.max_run_minutes or default
