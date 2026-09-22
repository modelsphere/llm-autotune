"""Search-space machinery the platform owns.

The search ALGORITHM is not here — it is an external policy container (see
docs/api/policy-contract.md). What lives here is everything the platform must
know about a space regardless of who searches it: how to expand and validate a
config (`space`, `validation`), the canonical point identity (`base`), how much
of a space a session has covered (`coverage`), and what a config leaves unsaid
that production sets (`parity`).
"""

from app.control.search.base import CandidateConfig

__all__ = ["CandidateConfig"]
