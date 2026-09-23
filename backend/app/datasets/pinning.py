"""Which dataset build a campaign measures against, and who is holding one.

The expensive stage replays real production traffic, and that traffic is
resampled every so often. Within one campaign that must not happen: candidates
are ranked against each other, so they have to be measured with the same
instrument. Across campaigns it is unavoidable and accepted — traffic really
does change — but it should be recorded rather than discovered.

A campaign therefore PINS a build at its first measurement and keeps it for its
whole life. Since the benchmark resolves the profile's *current* build, holding
the pin means not rebuilding the profile underneath it:

    a build is a shared read      every campaign pinned to it is a reader
    a rebuild is an exclusive write   it replaces what the profile points at

There is no lock table. "Who holds this build" is a question about campaigns,
and campaigns already answer it — a second place to store it is a second thing
to go stale when one is deleted or force-finished.

A campaign that arrives while someone else holds the current build ADOPTS it
rather than rebuilding: slightly older data, and the two campaigns become
directly comparable, which is worth more than the freshness. What it asked for
and what it got are both recorded, because "pinned D7 (adopted — campaign 27
was still running)" is an explanation, and a bare build id is not.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.datasets.profiles import DatasetBuild, same_dataset
from app.db.models import Campaign, CampaignStatus

# What a campaign may ask for at its first measurement.
REBUILD_AT_START = "rebuild_at_start"
USE_CURRENT = "use_current"
POLICIES = (REBUILD_AT_START, USE_CURRENT)

# What actually happened, which is not always what was asked.
APPLIED_REBUILT = "rebuilt"
APPLIED_ADOPTED = "adopted"
APPLIED_CURRENT = "current"
# The profile could not produce one — a build the collector deliberately
# refused, or a profile nobody has ever published to. Recorded so the campaign
# stops asking every tick and finishes on its screening evidence instead of
# waiting all night for a dataset that is not coming.
APPLIED_UNAVAILABLE = "unavailable"

# The decisions `decide()` can reach.
DECIDE_ADOPT = "adopt"
DECIDE_REBUILD = "rebuild"
DECIDE_TAKE_CURRENT = "take_current"

# Where the replay module reports what it actually replayed. Flat keys, module
# prefix included, exactly as they arrive.
METRIC_BUILD_ID = "replay.dataset_id"
METRIC_SHA = "replay.dataset_sha256"


def uses_pinning(campaign: Campaign) -> bool:
    return bool(campaign.dataset_profile)


def is_pinned(campaign: Campaign) -> bool:
    return bool(campaign.dataset_build_id)


def policy_of(campaign: Campaign) -> str:
    return campaign.dataset_policy or REBUILD_AT_START


def holders(session: Session, profile: str, build_id: str) -> list[Campaign]:
    """Campaigns that would be harmed by rebuilding this build.

    A campaign holds while it might still measure something — which includes
    PAUSED, since pausing means "back later" and resuming onto a swapped
    dataset is exactly the silent break this exists to prevent. That does mean
    a campaign parked indefinitely holds a build indefinitely; the way out is
    a deliberate, recorded, forced rebuild rather than an expiry nobody chose.
    """
    if not profile or not build_id:
        return []
    return list(
        session.scalars(
            select(Campaign).where(
                Campaign.dataset_profile == profile,
                Campaign.dataset_build_id == build_id,
                Campaign.status != CampaignStatus.DONE.value,
            )
        ).all()
    )


def decide(policy: str, current: DatasetBuild | None, held_by: list[Campaign]) -> str:
    """What to do at a campaign's first measurement.

    Nothing published yet forces a build whatever the policy says: `use_current`
    cannot be honoured when there is no current, and the alternative is a
    verification stage that fails at submit time with "no published build".
    """
    if current is None:
        return DECIDE_REBUILD
    if held_by:
        return DECIDE_ADOPT
    if policy == REBUILD_AT_START:
        return DECIDE_REBUILD
    return DECIDE_TAKE_CURRENT


def dataset_of(metrics: dict[str, Any]) -> tuple[str, str] | None:
    """The build a result was actually measured on, or None if it did not say.

    Only the replay module reports this. A screening result has no dataset and
    is not missing one.
    """
    build_id = str(metrics.get(METRIC_BUILD_ID) or "")
    sha = str(metrics.get(METRIC_SHA) or "")
    if not build_id and not sha:
        return None
    return build_id, sha


def mismatch(campaign: Campaign, metrics: dict[str, Any]) -> tuple[str, str] | None:
    """The dataset this result used, when it is not the one the campaign pinned.

    None means "nothing to report": the campaign does not pin, or the result
    carries no dataset, or it is the pinned one. A mismatch is not a failed
    run — the measurement is real, it just cannot be ranked against the others.
    """
    if not uses_pinning(campaign) or not is_pinned(campaign):
        return None
    reported = dataset_of(metrics)
    if reported is None:
        return None
    build_id, sha = reported
    if same_dataset(campaign.dataset_sha256, sha):
        return None
    if sha == "" and build_id == campaign.dataset_build_id:
        return None
    return build_id, sha


def comparable(campaign: Campaign, metrics: dict[str, Any]) -> bool:
    """May this result be ranked alongside the campaign's others?"""
    return mismatch(campaign, metrics) is None
