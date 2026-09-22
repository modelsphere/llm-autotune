"""Ranges and conditional parameters — the two things that stood between the
search space and an optimizer worth running.

A grid of hand-listed values is a search over the points someone already
suspected. A range says "anywhere in here", which is the only shape in which a
model-based policy has anything to model. A condition says "this parameter
only means something in company", which is what stops a night measuring the
same deployment four times under four different names.
"""

import pytest

from app.control.search import CandidateConfig
from app.control.search.space import (
    MAX_RANGE_VALUES,
    MAX_SPACE_CANDIDATES,
    axis_product,
    candidate_count,
    conditions,
    expand,
    prune_inactive,
    range_specs,
    space_errors,
    swept_keys,
    too_large,
)

# -- ranges -------------------------------------------------------------------


def test_a_range_expands_to_its_steps_inclusive_of_the_maximum():
    space = {"range": {"chunked_prefill_size": {"min": 2048, "max": 8192, "step": 2048}}}
    values = [c["chunked_prefill_size"] for c in expand(space)]
    assert values == [2048, 4096, 6144, 8192]
    assert candidate_count(space) == 4


def test_integer_ranges_stay_integers():
    """`--chunked-prefill-size 4096.0` is not what anyone typed, and it hashes
    differently from the same config written by hand."""
    space = {"range": {"max_running_requests": {"min": 16, "max": 64, "step": 16}}}
    values = [c["max_running_requests"] for c in expand(space)]
    assert values == [16, 32, 48, 64]
    assert all(isinstance(v, int) for v in values)


def test_float_ranges_are_rounded_to_the_steps_precision():
    """0.7 + 3*0.05 is 0.8500000000000001 in binary floating point. Rendered
    into a launch command that is a different flag value, and it hashes as a
    different candidate than the 0.85 someone else typed."""
    space = {"range": {"mem_fraction_static": {"min": 0.7, "max": 0.9, "step": 0.05}}}
    values = [c["mem_fraction_static"] for c in expand(space)]
    assert values == [0.7, 0.75, 0.8, 0.85, 0.9]


def test_a_range_composes_with_the_grid_as_one_more_axis():
    space = {
        "base": {"tp": 2},
        "grid": {"attention_backend": ["flashinfer", "triton"]},
        "range": {"chunked_prefill_size": {"min": 4096, "max": 16384, "step": 4096}},
    }
    configs = expand(space)
    assert len(configs) == 2 * 4 == candidate_count(space)
    assert all(c["tp"] == 2 for c in configs)
    assert set(swept_keys(space)) == {"attention_backend", "chunked_prefill_size"}


def test_a_range_is_offered_to_policies_as_an_interval_too():
    """An optimizer asking for an integer in [2048, 32768] step 2048 is doing
    something different from one picking among sixteen unrelated categories,
    even though they enumerate identically. TPE needs the former."""
    space = {"range": {"chunked_prefill_size": {"min": 2048, "max": 32768, "step": 2048}}}
    spec = range_specs(space)[0]
    assert (spec.name, spec.minimum, spec.maximum, spec.step) == (
        "chunked_prefill_size", 2048, 32768, 2048,
    )
    assert spec.is_integer
    assert len(spec.values()) == 16


def test_a_range_enumerates_exhaustively_too():
    """The step is mandatory precisely so this stays true: a range is also a
    finite set of points, which is what lets the platform say how much of a
    space a policy has covered."""
    space = {"range": {"mem_fraction_static": {"min": 0.8, "max": 0.9, "step": 0.05}}}
    assert [c["mem_fraction_static"] for c in expand(space)] == [0.8, 0.85, 0.9]


def test_a_range_without_a_step_is_rejected_with_the_reason():
    errors = space_errors({"range": {"mem_fraction_static": {"min": 0.7, "max": 0.9}}})
    assert any("needs a step" in e for e in errors)


@pytest.mark.parametrize(
    ("spec", "needle"),
    [
        ({"min": 0.9, "max": 0.7, "step": 0.05}, "below min"),
        ({"min": 0.7, "max": 0.9, "step": 0}, "step must be positive"),
        ({"min": 0, "max": 1, "step": 0.0001}, f"more than {MAX_RANGE_VALUES}"),
        ({"min": "x", "max": 0.9, "step": 0.05}, "numeric min and max"),
    ],
)
def test_bad_ranges_are_caught_before_saving(spec, needle):
    errors = space_errors({"range": {"mem_fraction_static": spec}})
    assert any(needle in e for e in errors), errors


def test_a_parameter_cannot_be_both_gridded_and_ranged():
    errors = space_errors({
        "grid": {"tp": [1, 2]},
        "range": {"tp": {"min": 1, "max": 4, "step": 1}},
    })
    assert any("appears in both" in e for e in errors)


# -- conditional parameters ---------------------------------------------------

SPEC_SPACE = {
    "base": {"tp": 2},
    "grid": {
        "speculative_algorithm": ["EAGLE", "NONE"],
        "speculative_num_steps": [3, 5],
    },
    "conditions": {"speculative_num_steps": {"speculative_algorithm": ["EAGLE"]}},
}


def test_an_inactive_parameter_is_dropped_from_the_candidate():
    active = prune_inactive(
        {"speculative_algorithm": "EAGLE", "speculative_num_steps": 3}, SPEC_SPACE
    )
    assert active == {"speculative_algorithm": "EAGLE", "speculative_num_steps": 3}

    inactive = prune_inactive(
        {"speculative_algorithm": "NONE", "speculative_num_steps": 3}, SPEC_SPACE
    )
    assert inactive == {"speculative_algorithm": "NONE"}


def test_conditions_collapse_configs_that_would_deploy_identically():
    """Without them this space is four candidates, two of which launch the
    exact same server — the engine ignores a draft-step count with speculation
    off. That is two machine-hours spent measuring one deployment twice."""
    configs = expand(SPEC_SPACE)
    assert len(configs) == 3 == candidate_count(SPEC_SPACE)
    assert configs.count({"tp": 2, "speculative_algorithm": "NONE"}) == 1
    assert sum(1 for c in configs if c["speculative_algorithm"] == "EAGLE") == 2


def test_the_collapsed_set_hashes_distinctly():
    configs = expand(SPEC_SPACE)
    assert len(configs) == 3
    hashes = {CandidateConfig(engine_args=c).hash for c in configs}
    assert len(hashes) == 3, "distinct configs, distinct hashes"


def test_pruning_happens_before_hashing():
    """Otherwise a conditional space is never exhausted: the same deployment
    is proposed under different hashes because an ignored parameter varies,
    and the campaign never recognizes it as already tried."""
    configs = expand(SPEC_SPACE)
    assert all(
        "speculative_num_steps" not in c
        for c in configs
        if c["speculative_algorithm"] == "NONE"
    )


def test_a_pruned_candidate_is_recognized_as_already_tried():
    """The same deployment proposed twice — once carrying a parameter the
    engine will ignore, once without it — is ONE point, because pruning
    happens before hashing. Without that, a conditional space is never
    exhausted: the ignored value varies and every repeat looks new."""
    tried = {CandidateConfig(engine_args=c).hash for c in expand(SPEC_SPACE)}
    # speculative_num_steps means nothing when the algorithm is NONE
    noisy = {"tp": 2, "speculative_algorithm": "NONE", "speculative_num_steps": 99}
    pruned = prune_inactive(noisy, SPEC_SPACE)
    assert "speculative_num_steps" not in pruned
    assert CandidateConfig(engine_args=pruned).hash in tried


def test_conditions_chain():
    """If a gating parameter is itself pruned, whatever it gates goes too."""
    space = {
        "base": {"a": 1, "b": 2, "c": 3},
        "grid": {"enable": [True, False]},
        "conditions": {"b": {"enable": [True]}, "c": {"b": [2]}},
    }
    assert prune_inactive({"enable": True, "b": 2, "c": 3}, space) == {
        "enable": True, "b": 2, "c": 3,
    }
    # enable off -> b pruned -> c's gate is gone, so c goes too.
    assert prune_inactive({"enable": False, "b": 2, "c": 3}, space) == {"enable": False}


def test_the_editors_string_values_still_match_a_condition():
    """The editor stores everything as text. Comparing strictly would drop a
    parameter the author meant to keep, and it would show up as an engine flag
    quietly missing from the launch command."""
    space = {"base": {"x": 1}, "grid": {"on": [True]}, "conditions": {"x": {"on": [True]}}}
    assert prune_inactive({"on": "true", "x": 1}, space) == {"on": "true", "x": 1}
    assert prune_inactive({"on": "false", "x": 1}, space) == {"on": "false"}

    numeric = {"base": {"x": 1}, "grid": {"n": [4096]}, "conditions": {"x": {"n": [4096]}}}
    assert prune_inactive({"n": "4096", "x": 1}, numeric) == {"n": "4096", "x": 1}


def test_a_condition_on_a_parameter_the_space_never_sets_is_rejected():
    """It would silently drop the parameter from every single candidate."""
    errors = space_errors({
        "grid": {"speculative_num_steps": [3]},
        "conditions": {"speculative_num_steps": {"speculative_algorithm": ["EAGLE"]}},
    })
    assert any("never sets" in e for e in errors)


def test_a_self_referential_condition_is_rejected():
    errors = space_errors({"grid": {"x": [1]}, "conditions": {"x": {"x": [1]}}})
    assert any("conditional on itself" in e for e in errors)


def test_a_condition_on_an_absent_parameter_is_rejected():
    errors = space_errors({"grid": {"a": [1]}, "conditions": {"ghost": {"a": [1]}}})
    assert any("not in this space" in e for e in errors)


def test_conditions_helper_normalizes_a_bare_value():
    assert conditions({"conditions": {"x": {"y": "EAGLE"}}}) == {"x": {"y": ["EAGLE"]}}


# -- consumers must not assume every candidate carries every swept key ---------


def test_a_swept_key_is_absent_from_the_candidates_it_is_gated_off_for():
    """The invariant that broke the preview. `swept_keys` lists what the space
    varies; a conditional parameter is varied and yet missing from the
    candidates whose gate does not match. Anything projecting candidates onto
    the swept keys has to tolerate that."""
    space = {
        "grid": {"attention_backend": ["flashinfer", "triton"]},
        "range": {"triton_attention_num_kv_splits": {"min": 4, "max": 16, "step": 4}},
        "conditions": {
            "triton_attention_num_kv_splits": {"attention_backend": ["triton"]}
        },
    }
    keys = swept_keys(space)
    assert "triton_attention_num_kv_splits" in keys

    configs = expand(space)
    flashinfer = [c for c in configs if c["attention_backend"] == "flashinfer"]
    assert flashinfer and all("triton_attention_num_kv_splits" not in c for c in flashinfer)


def test_the_preview_projection_tolerates_a_pruned_key():
    """Indexing blindly raised KeyError, the preview 500'd, and the editor —
    which swallows the failure and shows nothing — looked like it had hung."""
    from app.api.search_spaces import _swept_view

    swept = ["attention_backend", "triton_attention_num_kv_splits"]
    pruned = {"attention_backend": "flashinfer"}
    assert _swept_view(pruned, swept) == {"attention_backend": "flashinfer"}

    full = {"attention_backend": "triton", "triton_attention_num_kv_splits": 8}
    assert _swept_view(full, swept) == full

    # Nothing swept at all still shows the config rather than an empty row.
    assert _swept_view({"tp": 2}, []) == {"tp": 2}


# -- size, which intervals make possible to get wrong --------------------------


def test_a_space_too_large_to_enumerate_is_rejected_without_enumerating_it():
    """Hand-listed grids were self-limiting; intervals are not. Three ranges
    reach 134 million configurations, and the editor asks for a preview on
    every keystroke — discovering the size by expanding would hang the request
    that exists to report it."""
    huge = {
        "range": {
            "a": {"min": 0, "max": 511, "step": 1},
            "b": {"min": 0, "max": 511, "step": 1},
            "c": {"min": 0, "max": 511, "step": 1},
        }
    }
    assert too_large(huge)
    assert axis_product(huge) == 512**3

    errors = space_errors(huge)  # must return, not hang
    assert any("over the" in e and "limit" in e for e in errors)

    # And the count stays answerable without building the product.
    assert candidate_count(huge) == 512**3


def test_a_space_exactly_at_the_limit_is_still_allowed():
    """Two 50-value ranges crossed with a 2-value grid is 5,000 — the cap
    itself, not one over it. The per-range cap bites first for any single
    axis, so reaching the space cap takes several."""
    ok = {
        "grid": {"backend": ["flashinfer", "triton"]},
        "range": {
            "a": {"min": 1, "max": 50, "step": 1},
            "b": {"min": 1, "max": 50, "step": 1},
        },
    }
    assert axis_product(ok) == MAX_SPACE_CANDIDATES
    assert not too_large(ok)
    assert not space_errors(ok)

    one_more = {**ok, "grid": {"backend": ["flashinfer", "triton", "fa3"]}}
    assert too_large(one_more)


def test_conditions_do_not_trigger_expansion_of_an_oversized_space():
    """candidate_count expands when conditions are present, to report the
    collapsed number. It must not do that on a space it cannot expand."""
    huge = {
        "grid": {"gate": [True, False]},
        "range": {
            "a": {"min": 0, "max": 511, "step": 1},
            "b": {"min": 0, "max": 511, "step": 1},
        },
        "conditions": {"a": {"gate": [True]}},
    }
    assert candidate_count(huge) == 2 * 512 * 512  # the product, not an expansion


# -- the two together ---------------------------------------------------------


def test_a_realistic_space_is_large_enough_for_an_optimizer_to_matter():
    """The point of both features. Campaign 20's space was four points, which
    a policy can simply enumerate. This is the same sweep expressed as
    intervals: 2 x 16 x 5 = 160 configurations, of which a night can afford
    maybe twenty — so which twenty starts to matter.
    """
    space = {
        "base": {"tp": 2, "page_size": 64},
        "grid": {"attention_backend": ["flashinfer", "triton"]},
        "range": {
            "chunked_prefill_size": {"min": 2048, "max": 32768, "step": 2048},
            "mem_fraction_static": {"min": 0.8, "max": 0.9, "step": 0.025},
        },
    }
    assert candidate_count(space) == 2 * 16 * 5 == 160
    assert not space_errors(space)

    configs = expand(space)
    assert len(configs) == 160
    assert len({CandidateConfig(engine_args=c).hash for c in configs}) == 160
    for config in configs:
        assert 2048 <= config["chunked_prefill_size"] <= 32768
        assert 0.8 <= config["mem_fraction_static"] <= 0.9
