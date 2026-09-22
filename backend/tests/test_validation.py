"""Static validation is free to run and expensive to get wrong in either
direction: a missed rejection costs a machine-hour, a false one silently
deletes the configuration most worth trying."""

from app.control.search.validation import ValidationContext, cards_used, validate_config

CTX8 = ValidationContext(gpu_count=8, engine="sglang")


def test_parallelism_within_gpu_count_passes():
    assert validate_config({"tp_size": 4, "dp_size": 2}, CTX8) is None


def test_parallelism_exceeding_gpu_count_fails():
    error = validate_config({"tp_size": 8, "dp_size": 2}, CTX8)
    assert error is not None and "16 GPUs" in error


def test_vllm_style_arg_names_are_understood():
    error = validate_config({"tensor_parallel_size": 16}, CTX8)
    assert error is not None


def test_dp_attention_partitions_the_tp_group_instead_of_adding_replicas():
    """`--tp 4 --dp 4 --enable-dp-attention` is four GPUs, not sixteen: dp
    attention splits attention across ranks inside the tensor-parallel group.
    Multiplying rejects the standard MoE deployment."""
    config = {"tp": 4, "dp": 4, "enable_dp_attention": True}
    assert cards_used(config) == 4
    assert validate_config(config, CTX8) is None
    # …and the eight-card version of the same shape still fits exactly.
    assert cards_used({"tp": 8, "dp": 8, "enable_dp_attention": True}) == 8


def test_data_parallel_replicas_still_multiply_without_dp_attention():
    assert cards_used({"tp": 4, "dp": 4}) == 16
    assert validate_config({"tp": 4, "dp": 4}, CTX8) is not None


def test_the_editors_string_booleans_are_read_as_booleans():
    """The search-space editor stores switches as "true"/"false", and "false"
    is truthy in Python — reading it naively would fold dp into tp for a
    config that never asked for it."""
    assert cards_used({"tp": 2, "dp": 2, "enable_dp_attention": "true"}) == 2
    assert cards_used({"tp": 2, "dp": 2, "enable_dp_attention": "false"}) == 4


def test_dp_attention_needs_dp_to_divide_tp():
    error = validate_config({"tp": 4, "dp": 3, "enable_dp_attention": True}, CTX8)
    assert error is not None and "divide" in error


def test_expert_parallel_shards_the_cards_the_config_already_has():
    """`--ep` never adds GPUs, so it must not be multiplied in — but asking for
    more expert shards than ranks is a startup error worth catching."""
    assert cards_used({"tp": 4, "ep": 4}) == 4
    assert validate_config({"tp": 4, "ep": 4}, CTX8) is None
    error = validate_config({"tp": 4, "ep": 8}, CTX8)
    assert error is not None and "ep=8" in error


def test_mem_fraction_bounds():
    assert validate_config({"mem_fraction_static": 0.9}, CTX8) is None
    assert validate_config({"mem_fraction_static": 1.5}, CTX8) is not None
    assert validate_config({"gpu_memory_utilization": 0.05}, CTX8) is not None


def test_non_positive_ints_rejected():
    assert validate_config({"tp_size": 0}, CTX8) is not None


def test_garbage_values_are_rejected_not_raised():
    """A bad value in a search space must fail this candidate, not the planner
    tick that was proposing every other candidate with it."""
    assert validate_config({"tp": "auto"}, CTX8) is not None
    assert validate_config({"mem_fraction_static": "high"}, CTX8) is not None
