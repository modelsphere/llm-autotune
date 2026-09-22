"""The scheduling policy, isolated from the database.

A grid mixing tp=2 and tp=4 gives jobs of different widths. Which one starts
next decides how many rounds the night takes, and whether a wide job ever
runs at all.
"""

from app.control.orchestrator.packing import (
    Placement,
    choose_next,
    free_indices,
    rounds_needed,
)


def test_the_widest_candidate_that_fits_goes_first():
    """Narrowest-first would refill the machine with tp=2s every round and run
    the tp=4 last — slower, and the opposite of what a watcher expects."""
    pending = [Placement(1, 2), Placement(2, 4), Placement(3, 2)]
    assert choose_next(8, pending) == Placement(2, 4)


def test_a_candidate_that_does_not_fit_is_skipped_not_squeezed():
    pending = [Placement(1, 4), Placement(2, 2)]
    assert choose_next(2, pending) == Placement(2, 2)
    assert choose_next(1, pending) is None


def test_ties_break_on_id_so_the_order_is_reproducible():
    pending = [Placement(7, 2), Placement(3, 2)]
    assert choose_next(4, pending).candidate_id == 3


def test_a_zero_width_candidate_is_never_placed():
    """cards_used returning 0 would otherwise "fit" anywhere and loop."""
    assert choose_next(8, [Placement(1, 0)]) is None


def test_free_indices_are_the_cards_no_live_run_holds():
    assert free_indices(8, {0, 1, 4}) == [2, 3, 5, 6, 7]
    assert free_indices(4, {0, 1, 2, 3}) == []


def test_rounds_needed_counts_full_machine_passes():
    # 4 candidates of tp=2 on 8 cards is one round, not four.
    assert rounds_needed(8, [2, 2, 2, 2]) == 1
    assert rounds_needed(8, [4, 4, 2, 2]) == 2
    assert rounds_needed(8, []) == 0
    # No cards reported (CPU-only host): fall back to one round per candidate.
    assert rounds_needed(0, [1, 1, 1]) == 3
