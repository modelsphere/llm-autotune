"""Engine adapters: how a model is served, separated from how a machine is
reached. The ssh driver and the future k8s driver must get the same argv."""

import pytest

from app.control.engines import MODEL_MOUNT, get_adapter, render_args
from app.control.launch import LaunchSpec, MachineInfo
from app.control.launch.ssh_docker import render_engine_command


def _spec(engine: str = "sglang", **kwargs) -> LaunchSpec:
    return LaunchSpec(
        run_id=42,
        machine=MachineInfo(name="gpu-01", host="10.0.0.1", gpu_count=8),
        engine=engine,
        image="lmsysorg/sglang:latest",
        model_path="/mnt/disk0/models/qwen3-32b",
        served_model_name="qwen3-32b",
        engine_args={"tp_size": 4, "enable_torch_compile": True},
        **kwargs,
    )


def test_the_driver_delegates_to_the_adapter():
    """The shim exists so nothing that imported the old function broke; it
    must not become a second, drifting implementation."""
    spec = _spec()
    assert render_engine_command(spec) == get_adapter("sglang").build_command(spec)


def test_each_engine_gets_its_own_invocation():
    assert get_adapter("sglang").build_command(_spec())[:3] == [
        "python3", "-m", "sglang.launch_server",
    ]
    # vllm takes the model positionally rather than behind --model-path.
    assert get_adapter("vllm").build_command(_spec(engine="vllm"))[:3] == [
        "vllm", "serve", MODEL_MOUNT,
    ]


def test_an_unknown_engine_is_refused_by_name():
    with pytest.raises(ValueError, match="unknown engine"):
        get_adapter("tensorrt")


def test_a_true_boolean_is_a_bare_flag_and_a_false_one_is_absent():
    """`--enable-x False` is parsed as enabled by both engines' argparse, so a
    false switch has to be omitted entirely rather than rendered."""
    assert render_args({"enable_x": True}) == ["--enable-x"]
    assert render_args({"enable_x": False}) == []


def test_arguments_render_in_a_stable_order():
    """A launch command that reordered itself would look like a different
    deployment every time it was compared against a production capture."""
    forwards = render_args({"tp_size": 4, "mem_fraction_static": 0.9})
    backwards = render_args({"mem_fraction_static": 0.9, "tp_size": 4})
    assert forwards == backwards


def test_snake_case_is_translated_only_at_this_boundary():
    """Search spaces, the catalog and the validator all speak snake_case; the
    --flag-name form exists only in the rendered command."""
    assert render_args({"mem_fraction_static": 0.9}) == ["--mem-fraction-static", "0.9"]


def test_the_model_mount_matches_what_the_driver_binds():
    """The adapter passes --model-path /model and the driver is what puts the
    weights there. Two copies of the constant would drift into a launch that
    cannot find the model, with nothing in the config to explain it."""
    from app.control.launch import ssh_docker

    assert ssh_docker.MODEL_MOUNT is MODEL_MOUNT
    assert "--model-path" in get_adapter("sglang").build_command(_spec())


def test_stored_keys_render_in_each_engines_own_spelling():
    # One canonical vocabulary, two dialects: the same stored `tp` must reach
    # sglang as --tp-size and vllm as --tensor-parallel-size (vllm registers
    # -tp, not --tp — the generic kebab would not even parse there). Alias
    # keys from older stored configs resolve the same way.
    assert render_args({"tp": 2}, engine="sglang") == ["--tp-size", "2"]
    assert render_args({"tp": 2}, engine="vllm") == ["--tensor-parallel-size", "2"]
    assert render_args({"tensor_parallel_size": 2}, engine="sglang") == ["--tp-size", "2"]
    # Unknown keys keep the generic rendering — passthrough, never a gate.
    assert render_args({"mamba_full_memory_ratio": "4.59"}, engine="sglang") == [
        "--mamba-full-memory-ratio", "4.59",
    ]


def test_false_renders_the_engines_own_negation_or_nothing():
    # vllm gives EVERY bool a --no- form (BooleanOptionalAction, engine-wide);
    # sglang only the negatable few — anywhere else False must render as
    # nothing, because an invented --no-x on a store_true flag crashes the
    # launch.
    assert render_args({"enable_prefix_caching": False}, engine="vllm") == [
        "--no-enable-prefix-caching",
    ]
    assert render_args({"enable_metrics": False}, engine="sglang") == []
    assert render_args({"enable_multimodal": False}, engine="sglang") == [
        "--no-enable-multimodal",
    ]


def test_normalize_folds_aliases_and_negations_into_the_canonical():
    from app.control.engines.flags import normalize_args

    args, notes = normalize_args(
        "sglang",
        {"tensor_parallel_size": "2", "no_enable_multimodal": True,
         "trust_remote_code": "false", "no_brand_new_flag": True},
    )
    assert args["tp"] == "2"
    assert args["enable_multimodal"] is False, "a known switch's --no- form folds"
    assert args["trust_remote_code"] is False, "the word false becomes the boolean"
    assert args["no_brand_new_flag"] is True, (
        "an unknown --no- flag stays as pasted — passthrough beats a guessed negation"
    )
    assert notes, "every rewrite is named, never silent"


def test_the_mamba_strategy_flag_keeps_the_spelling_every_build_accepts():
    """sglang renamed --mamba-scheduler-strategy to --mamba-radix-cache-strategy
    and still accepts the old one. The OLD spelling stays canonical: it is what
    campaign 20's byte-identical command carries and what an older image
    understands; the new spelling folds into it rather than the other way."""
    from app.control.engines.flags import normalize_args

    folded, notes = normalize_args("sglang", {"mamba_radix_cache_strategy": "extra_buffer"})
    assert folded == {"mamba_scheduler_strategy": "extra_buffer"}
    assert notes == ["mamba_radix_cache_strategy → mamba_scheduler_strategy"]
    assert render_args(folded, engine="sglang") == ["--mamba-scheduler-strategy", "extra_buffer"]
