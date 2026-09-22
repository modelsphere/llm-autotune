"""A search space expands one way, and everything asks the same expander.

Tied groups exist because a cartesian product is the wrong shape when two
parameters are not independent: a memory fraction that is safe at tp=4 may OOM
at tp=1, so 3 tp values × 3 fractions is nine candidates of which most are
either wasted or dead on arrival. Tying them gives the three the user meant.
"""

from app.control.search.space import (
    candidate_count,
    expand,
    space_errors,
    swept_keys,
)

TIED = {
    "base": {"page_size": 64},
    "tied": [{"tp_size": [1, 2, 4], "mem_fraction_static": [0.9, 0.85, 0.8]}],
}


def test_a_tied_group_zips_instead_of_crossing():
    configs = expand(TIED)
    assert [(c["tp_size"], c["mem_fraction_static"]) for c in configs] == [
        (1, 0.9),
        (2, 0.85),
        (4, 0.8),
    ]
    assert all(c["page_size"] == 64 for c in configs), "base is carried into every candidate"
    assert candidate_count(TIED) == 3


def test_a_tied_group_crosses_the_rest_of_the_grid_as_one_axis():
    """Three tied pairs × two backends is six candidates, not eighteen: the
    group behaves as a single parameter whose values happen to be tuples."""
    space = {**TIED, "grid": {"attention_backend": ["flashinfer", "triton"]}}
    configs = expand(space)
    assert len(configs) == 6 == candidate_count(space)
    pairs = {(c["tp_size"], c["mem_fraction_static"]) for c in configs}
    assert pairs == {(1, 0.9), (2, 0.85), (4, 0.8)}


def test_a_plain_grid_is_still_a_cartesian_product():
    space = {"grid": {"tp_size": [1, 2, 4], "mem_fraction_static": [0.9, 0.85, 0.8]}}
    assert candidate_count(space) == 9
    assert len(expand(space)) == 9


def test_swept_keys_come_from_the_declaration_not_the_candidates():
    """What varies is declared, not inferred: a one-value axis is still a swept
    parameter, and diffing candidates would call it fixed."""
    assert swept_keys({**TIED, "grid": {"attention_backend": ["triton"]}}) == [
        "attention_backend",
        "mem_fraction_static",
        "tp_size",
    ]


def test_an_empty_space_is_one_candidate():
    assert expand({"base": {"tp_size": 2}}) == [{"tp_size": 2}]
    assert candidate_count({}) == 1


def test_a_ragged_tied_group_is_reported_before_it_is_saved():
    ragged = {"tied": [{"tp_size": [1, 2, 4], "mem_fraction_static": [0.9, 0.85]}]}
    errors = space_errors(ragged)
    assert errors and "same number of values" in errors[0]
    # …and expansion still does something sane with it rather than raising
    # into an empty night: the unpaired tail is dropped.
    assert len(expand(ragged)) == 2


def test_a_parameter_cannot_be_both_tied_and_swept_alone():
    """Crossing a parameter with itself produces pairs the user never asked
    for, and which value wins depends on axis order."""
    clash = {"grid": {"tp_size": [1, 2]}, "tied": [{"tp_size": [1, 2], "dp_size": [2, 1]}]}
    assert any("both the grid" in e for e in space_errors(clash))


def test_a_group_of_one_is_a_grid_axis_in_disguise():
    assert any("fewer than two" in e for e in space_errors({"tied": [{"tp_size": [1, 2]}]}))


def test_the_expansion_is_exactly_what_the_space_declares():
    """The count shown before the campaign starts and the points the space
    actually holds must be one derivation, not two that can drift."""
    points = expand(TIED)
    assert len(points) == candidate_count(TIED)
    assert [p["tp_size"] for p in points] == [1, 2, 4]
