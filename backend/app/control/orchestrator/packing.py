"""Which candidate to start next, given the cards currently free on a machine.

This is a scheduling decision, not a search one: the Planner decides WHAT to
try, this decides WHEN and WHERE it runs. Keeping them apart is what lets a
new search algorithm drop in without touching machine allocation.

The problem: a grid that mixes tp=2 and tp=4 gives jobs of different widths,
and an 8-card node running them one at a time finishes in as many rounds as
there are candidates. Runs are treated as equal-length here — deliberately,
because the whole point of the campaign is that we do not yet know how long a
config takes, and guessing would bias the schedule toward configs we happen to
have measured before. With equal durations, minimizing makespan is bin packing:
the floor is ceil(sum(cards) / gpu_count) rounds.

First-fit-decreasing reaches that floor on the shapes that actually occur
(4+4, 4+2+2, 2+2+2+2 on 8 cards) and, more importantly, has the property that
matters operationally: because it always takes the WIDEST candidate that fits,
a tp=4 job cannot starve behind a queue of tp=2 jobs that keep refilling the
machine. Taking the narrowest instead would fill 8 cards with four tp=2 runs
every round and run the tp=4s last, which is both slower and the opposite of
what someone watching the campaign expects.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Placement:
    """One candidate, and the cards it would occupy."""

    candidate_id: int
    cards: int


def choose_next(free_cards: int, pending: list[Placement]) -> Placement | None:
    """The widest pending candidate that fits in `free_cards`, or None.

    Ties break on candidate id so a campaign still works through its grid in
    a predictable order, and so the choice is reproducible from the database.
    """
    fitting = [p for p in pending if 0 < p.cards <= free_cards]
    if not fitting:
        return None
    return min(fitting, key=lambda p: (-p.cards, p.candidate_id))


def free_indices(gpu_count: int, taken: set[int]) -> list[int]:
    """Card indices not currently held by a live run, lowest first."""
    return [index for index in range(gpu_count) if index not in taken]


def rounds_needed(gpu_count: int, cards: list[int]) -> int:
    """Lower bound on how many full-machine rounds a set of candidates needs.

    Used for the wall-clock estimate shown before a campaign starts: with
    sharing on, 8 candidates of tp=2 on an 8-card node is 2 rounds, not 8.
    """
    if gpu_count <= 0:
        return len(cards)
    total = sum(max(1, c) for c in cards)
    widest = max(cards, default=0)
    # Cannot beat either the total-cards bound or "one round per widest job".
    return max(-(-total // gpu_count), 1 if widest else 0)
