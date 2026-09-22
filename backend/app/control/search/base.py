"""One point in a search space, and how it is identified.

Search itself lives OUTSIDE the platform: a campaign names a policy container
that proposes configs through the policy session API. What stays here is the
platform's own coordinate system — the canonical form of a config and the hash
that says whether two proposals are the same point — because the platform, not
the policy, decides what counts as already tried.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CandidateConfig:
    """One point in the search space: engine args merged from base + proposal."""

    engine_args: dict[str, Any]

    @property
    def hash(self) -> str:
        canon = json.dumps(self.engine_args, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canon.encode()).hexdigest()[:16]
