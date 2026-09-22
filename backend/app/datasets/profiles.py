"""LLMBench replay-dataset profiles — the instrument the expensive stage measures with.

A replay benchmark resolves `dataset_source: auto` against a named collection
profile, and takes whatever build that profile is currently pointing at. The
profiles LLMBench collects on its own schedule roll every 24 hours, which is
correct for them and wrong for us: a campaign spans several nights and ranks
configs against each other, so a rebuild landing mid-campaign silently swaps
the measuring stick. Two scores from two builds are not a comparison.

So a profile is set aside for this platform with `schedule_interval_hours: 0`
— nothing rebuilds it but the call below. That is the whole coupling: we ask
for a build, we are told its name, and we read that name back off every
result. We never learn how a dataset is collected and never fetch one; the
ids are opaque and LLMBench owns everything behind them.

  create   done once, by hand, on the LLMBench side (schedule_interval_hours 0)
  build    POST /replay-datasets/profiles/{id}/build   -> {build_row_id}
           409 = one already in flight; one per profile, by design
  poll     GET  /replay-datasets/builds/{row}          -> pending|running|ready|failed
  read     GET  /replay-datasets/profiles/{id}         -> current build + summary
  trace    every submission returns dataset_id/dataset_sha256 in its metrics

Contract verified against the live platform 2026-08-05 (profile
`prod-traffic-sample`, build 20260805T091004Z).
"""

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.evaluation.llmbench import LLMBenchClient

logger = logging.getLogger(__name__)

# What a submission reports back is a PREFIX of what the profile API returns:
# metrics carry `dataset_sha256` truncated to 16 characters, the profile and
# build endpoints carry all 64. Comparing them whole never matches.
METRIC_SHA_LENGTH = 16


class DatasetProfileError(RuntimeError):
    """The profile API said no in a way worth repeating to a human."""


class BuildInFlight(DatasetProfileError):
    """409 — this profile is already building. Not an error; wait for it."""


@dataclass(frozen=True)
class DatasetBuild:
    """One published build: what a campaign pins, and what a run must report."""

    build_id: str
    sha256: str
    records: int
    built_at: str = ""
    window_start: str = ""
    window_end: str = ""

    @property
    def metric_sha(self) -> str:
        """The form a benchmark result reports."""
        return self.sha256[:METRIC_SHA_LENGTH]

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "DatasetBuild | None":
        build_id = raw.get("build_id") or ""
        if not build_id:
            return None
        return cls(
            build_id=build_id,
            sha256=raw.get("sha256") or "",
            records=int(raw.get("records") or 0),
            built_at=raw.get("built_at") or raw.get("finished_at") or "",
            window_start=raw.get("window_start") or "",
            window_end=raw.get("window_end") or "",
        )


def same_dataset(pinned_sha: str, reported_sha: str) -> bool:
    """Does a result's dataset match the pin, given the two lengths involved?

    Compared on the shorter of the two, always — a full hash and its own
    16-character prefix are the same dataset, and treating them as different
    would flag every single run.
    """
    if not pinned_sha or not reported_sha:
        return False
    n = min(len(pinned_sha), len(reported_sha))
    return pinned_sha[:n] == reported_sha[:n]


class DatasetProfileClient:
    """Drives one externally-managed collection profile on LLMBench."""

    def __init__(self, client: LLMBenchClient | None = None):
        self.bench = client or LLMBenchClient()

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        return self.bench.request(method, path, **kwargs)

    # -- reading ------------------------------------------------------------

    def list_profiles(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/replay-datasets/profiles")
        if response.status_code >= 400:
            raise DatasetProfileError(
                f"could not list dataset profiles ({response.status_code}): {response.text[:200]}"
            )
        body = response.json()
        profiles = body.get("profiles", body) if isinstance(body, dict) else body
        return [p for p in (profiles or []) if isinstance(p, dict)]

    def names(self) -> list[str]:
        return [p["name"] for p in self.list_profiles() if p.get("name")]

    def find(self, name: str) -> dict[str, Any] | None:
        """The profile with this name, or None if the platform has no such thing.

        Names, not ids: an id is a row number on someone else's database, and
        a campaign that pins one would silently follow a renumbering. The name
        is immutable there by design — it names a directory on their volume.
        """
        for profile in self.list_profiles():
            if profile.get("name") == name:
                return profile
        return None

    def current(self, profile: dict[str, Any]) -> DatasetBuild | None:
        """What a submission would replay right now. None = nothing published yet."""
        return DatasetBuild.from_api(profile.get("current") or {})

    # -- driving ------------------------------------------------------------

    def trigger_build(self, profile_id: int) -> int:
        """Ask for a fresh build; returns the row id to poll.

        A 409 means someone already asked and it is running — which is a fine
        outcome, not a failure, so it gets its own exception rather than being
        flattened into "the platform said no".
        """
        response = self._request("POST", f"/replay-datasets/profiles/{profile_id}/build")
        if response.status_code == 409:
            raise BuildInFlight(response.text[:200])
        if response.status_code >= 400:
            raise DatasetProfileError(
                f"build refused ({response.status_code}): {response.text[:200]}"
            )
        return int(response.json()["build_row_id"])

    def latest_build_row(self, profile_id: int) -> int:
        """The newest build row, whoever asked for it. 0 if there are none.

        Used after a 409: something is already building, and polling *that*
        row is the difference between waiting for it and asking again every
        tick until it publishes and then triggering a second one.
        """
        response = self._request(
            "GET", f"/replay-datasets/profiles/{profile_id}/builds", params={"limit": 1}
        )
        if response.status_code >= 400:
            return 0
        body = response.json()
        builds = body.get("builds", body) if isinstance(body, dict) else body
        for build in builds or []:
            if build.get("id"):
                return int(build["id"])
        return 0

    def build(self, build_row_id: int) -> dict[str, Any]:
        """One build's state: status in pending|running|ready|failed."""
        response = self._request("GET", f"/replay-datasets/builds/{build_row_id}")
        if response.status_code >= 400:
            raise DatasetProfileError(
                f"could not read build {build_row_id} "
                f"({response.status_code}): {response.text[:200]}"
            )
        return response.json()
