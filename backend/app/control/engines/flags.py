"""The engines' flag vocabulary: aliases, switches, and per-engine spellings.

One flag, many spellings: sglang's tensor parallelism answers to `--tp-size`,
`--tensor-parallel-size` and the `--tp` abbreviation; vllm spells the same
knob `--tensor-parallel-size` with a `-tp` short form. The platform stores ONE
canonical snake_case key per flag — shared across engines where the meaning is
identical (`tp`, `dp`, `pp`: the search-space vocabulary campaigns already
use) and per-engine everywhere else (`mem_fraction_static` is not
`gpu_memory_utilization`; near-synonyms with different definitions stay
apart). Aliases fold into the canonical at intake; the engine's own primary
spelling comes back out at render.

Booleans differ by engine, verified against both arg parsers (2026-09-01):
- vllm generates every plain bool field with argparse.BooleanOptionalAction,
  so `--enable-x` and `--no-enable-x` both exist, engine-wide.
- sglang generates plain bools as store_true ONLY; a specific few fields are
  declared BooleanOptionalAction and accept the `--no-` form. So a canonical
  False renders `--no-<flag>` on vllm always, on sglang only where the spec
  says `negatable` — anywhere else False is omitted, which for a store_true
  flag means exactly "off" (emitting an unknown `--no-x` would crash the
  launch).

The catalog stays ADVISORY for unknown flags: a key with no spec renders
generically (snake → --kebab) and rides along untouched, so a brand-new
engine flag needs nothing from us. A pasted `--no-foo` whose `foo` we do not
know stays `no_foo: true` — perfect passthrough beats a guessed negation.
"""

from dataclasses import dataclass
from functools import cache
from typing import Any


@dataclass(frozen=True)
class FlagSpec:
    key: str                            # canonical snake_case key (stored form)
    kind: str = "value"                 # "value" | "switch"
    aliases: tuple[str, ...] = ()       # other snake_case spellings, folded in
    flag: str = ""                      # CLI spelling; "" = --<key with dashes>
    short: tuple[str, ...] = ()         # single-dash forms ("-tp"), parse-only
    negatable: bool = False             # sglang only: --no-<flag> exists


def _kebab(key: str) -> str:
    return "--" + key.replace("_", "-")


# How a canonical False renders: vllm's parser gives EVERY bool a --no- form;
# sglang only the specs marked negatable.
_NEGATION_POLICY = {"vllm": "always", "sglang": "catalog"}

SPECS: dict[str, tuple[FlagSpec, ...]] = {
    "sglang": (
        # -- parallelism / placement (tp/dp/pp/ep: the cross-engine core) ----
        FlagSpec("tp", flag="--tp-size", aliases=("tp_size", "tensor_parallel_size")),
        FlagSpec("dp", flag="--dp-size", aliases=("dp_size", "data_parallel_size")),
        FlagSpec("pp", flag="--pp-size", aliases=("pp_size", "pipeline_parallel_size")),
        FlagSpec("ep", flag="--ep-size", aliases=("ep_size", "expert_parallel_size")),
        FlagSpec("dcp_size", aliases=("decode_context_parallel_size",)),
        FlagSpec("attn_cp_size", aliases=("attention_context_parallel_size",)),
        FlagSpec("moe_dp_size", aliases=("moe_data_parallel_size",)),
        FlagSpec("enable_dp_attention", kind="switch", aliases=("enable_dp_attn",)),
        # -- memory / kv cache ----------------------------------------------
        FlagSpec("mem_fraction_static"),
        FlagSpec("max_total_tokens"),
        FlagSpec("kv_cache_dtype"),
        FlagSpec("page_size"),
        FlagSpec("disable_radix_cache", kind="switch"),
        FlagSpec("radix_eviction_policy"),
        # -- batching / scheduling ------------------------------------------
        FlagSpec("chunked_prefill_size"),
        FlagSpec("max_prefill_tokens"),
        FlagSpec("max_running_requests"),
        FlagSpec("schedule_policy"),
        FlagSpec("schedule_conservativeness"),
        FlagSpec("enable_mixed_chunk", kind="switch"),
        FlagSpec("num_continuous_decode_steps"),
        # -- backends --------------------------------------------------------
        FlagSpec("attention_backend"),
        FlagSpec("sampling_backend"),
        FlagSpec("grammar_backend"),
        # -- compilation / graphs -------------------------------------------
        FlagSpec("enable_torch_compile", kind="switch"),
        FlagSpec("torch_compile_max_bs"),
        FlagSpec("cuda_graph_max_bs"),
        FlagSpec("cuda_graph_bs"),
        FlagSpec("disable_cuda_graph", kind="switch"),
        FlagSpec("disable_cuda_graph_padding", kind="switch"),
        # -- model / dtype ---------------------------------------------------
        FlagSpec("dtype"),
        FlagSpec("quantization"),
        FlagSpec("context_length"),
        FlagSpec("trust_remote_code", kind="switch"),
        FlagSpec("revision"),
        FlagSpec("tokenizer_path"),
        FlagSpec("tokenizer_mode"),
        # `--no-enable-multimodal` is live in the 0.5.x builds we serve
        # (BooleanOptionalAction there; Optional[bool] upstream).
        FlagSpec("enable_multimodal", kind="switch", negatable=True),
        # -- hybrid mamba / linear attention (Qwen3.8, Nemotron-H, …) --------
        # Verified against sglang v0.5.15 server_args: every one a plain
        # value. sglang renamed --mamba-scheduler-strategy to
        # --mamba-radix-cache-strategy and still accepts the old spelling; the
        # OLD one stays canonical here because it is the one every build we
        # have run accepts (campaign 20's byte-identical command carries it),
        # while the new one would fail to launch on an older image. Flip the
        # two when sglang drops the alias.
        FlagSpec("mamba_ssm_dtype"),
        FlagSpec("mamba_full_memory_ratio"),
        FlagSpec("mamba_scheduler_strategy", aliases=("mamba_radix_cache_strategy",)),
        FlagSpec("max_mamba_cache_size"),
        # -- speculative decoding -------------------------------------------
        FlagSpec("speculative_algorithm"),
        FlagSpec("speculative_draft_model_path", aliases=("speculative_draft_model",)),
        FlagSpec("speculative_num_draft_tokens"),
        FlagSpec("speculative_num_steps"),
        FlagSpec("speculative_eagle_topk"),
        # -- parsers / api behaviour ----------------------------------------
        FlagSpec("reasoning_parser"),
        FlagSpec("tool_call_parser"),
        FlagSpec("chat_template"),
        FlagSpec("stream_response_default_include_usage", kind="switch"),
        FlagSpec("enable_cache_report", kind="switch"),
        FlagSpec("enable_metrics", kind="switch"),
        FlagSpec("show_time_cost", kind="switch"),
        FlagSpec("log_level"),
        FlagSpec("log_requests", kind="switch"),
        # -- misc operational ------------------------------------------------
        FlagSpec("random_seed"),
        FlagSpec("watchdog_timeout"),
        FlagSpec("enable_p2p_check", kind="switch"),
        FlagSpec("allow_auto_truncate", kind="switch"),
    ),
    "vllm": (
        # -- parallelism / placement ----------------------------------------
        FlagSpec("tp", flag="--tensor-parallel-size",
                 aliases=("tensor_parallel_size", "tp_size"), short=("-tp",)),
        FlagSpec("dp", flag="--data-parallel-size",
                 aliases=("data_parallel_size", "dp_size"), short=("-dp",)),
        FlagSpec("pp", flag="--pipeline-parallel-size",
                 aliases=("pipeline_parallel_size", "pp_size"), short=("-pp",)),
        FlagSpec("enable_expert_parallel", kind="switch"),
        FlagSpec("distributed_executor_backend"),
        # -- memory / kv cache ----------------------------------------------
        FlagSpec("gpu_memory_utilization"),
        FlagSpec("swap_space"),
        FlagSpec("cpu_offload_gb"),
        FlagSpec("block_size"),
        FlagSpec("kv_cache_dtype"),
        FlagSpec("enable_prefix_caching", kind="switch"),
        # -- batching / scheduling ------------------------------------------
        FlagSpec("max_num_seqs"),
        FlagSpec("max_num_batched_tokens"),
        FlagSpec("enable_chunked_prefill", kind="switch"),
        FlagSpec("scheduler_delay_factor"),
        FlagSpec("max_seq_len_to_capture"),
        # -- model / dtype ---------------------------------------------------
        FlagSpec("dtype"),
        FlagSpec("quantization", short=("-q",)),
        FlagSpec("max_model_len"),
        FlagSpec("trust_remote_code", kind="switch"),
        FlagSpec("revision"),
        FlagSpec("tokenizer"),
        FlagSpec("tokenizer_mode"),
        FlagSpec("load_format"),
        FlagSpec("seed"),
        # -- execution -------------------------------------------------------
        FlagSpec("enforce_eager", kind="switch"),
        FlagSpec("disable_custom_all_reduce", kind="switch"),
        FlagSpec("compilation_config", short=("-cc",)),
        # -- speculative decoding -------------------------------------------
        FlagSpec("speculative_config", short=("-sc",)),
        FlagSpec("speculative_model"),
        FlagSpec("num_speculative_tokens"),
        FlagSpec("speculative_draft_tensor_parallel_size"),
        # -- lora / api behaviour -------------------------------------------
        FlagSpec("enable_lora", kind="switch"),
        FlagSpec("max_loras"),
        FlagSpec("max_lora_rank"),
        FlagSpec("disable_log_requests", kind="switch"),
        FlagSpec("disable_log_stats", kind="switch"),
        FlagSpec("uvicorn_log_level"),
    ),
}

_TRUE_WORDS = frozenset({"", "true", "1", "yes", "on"})
_FALSE_WORDS = frozenset({"false", "0", "no", "off"})


@cache
def _alias_map(engine: str) -> dict[str, str]:
    """Every accepted snake_case spelling → the canonical key (identity incl.)."""
    out: dict[str, str] = {}
    for spec in SPECS.get(engine, ()):
        out[spec.key] = spec.key
        for alias in spec.aliases:
            out[alias] = spec.key
    return out


@cache
def _spec_map(engine: str) -> dict[str, FlagSpec]:
    return {spec.key: spec for spec in SPECS.get(engine, ())}


@cache
def short_flags(engine: str) -> dict[str, str]:
    """Single-dash short forms ("-tp") → the flag's canonical key."""
    out: dict[str, str] = {}
    for spec in SPECS.get(engine, ()):
        for form in spec.short:
            out[form] = spec.key
    return out


@cache
def known_flags(engine: str) -> frozenset[str]:
    """Every spelling the catalog recognizes — advisory, never a gate."""
    return frozenset(_alias_map(engine))


def spec_of(engine: str, key: str) -> FlagSpec | None:
    canonical = _alias_map(engine).get(key)
    return _spec_map(engine).get(canonical) if canonical else None


def render_flag(engine: str, key: str) -> str:
    """The engine's primary CLI spelling for a stored key — the canonical
    spec's spelling when the key (or an alias of it) is known, the generic
    snake→kebab everywhere else."""
    spec = spec_of(engine, key)
    if spec is not None:
        return spec.flag or _kebab(spec.key)
    return _kebab(key)


def negated_flag(engine: str, key: str) -> str | None:
    """How to spell "explicitly off", or None when the engine has no such
    spelling and omission is the only honest rendering."""
    spec = spec_of(engine, key)
    policy = _NEGATION_POLICY.get(engine, "catalog")
    if policy == "always" or (spec is not None and spec.negatable):
        flag = spec.flag if spec is not None and spec.flag else _kebab(
            spec.key if spec is not None else key
        )
        return "--no-" + flag.lstrip("-")
    return None


def normalize_args(engine: str, args: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """One dict in the canonical vocabulary, and the notes saying what moved.

    - alias keys fold into the canonical (`tensor_parallel_size` → `tp`);
    - a parsed `--no-x` (key `no_x`) becomes `x: False` when x is a known
      switch — unknown ones stay `no_x: True`, the exact CLI preserved;
    - on known switches, the words "true"/"false"/"1"/"0"/… become real
      booleans (a store_true flag rendered `--enable-x false` would crash
      the launch, so the strings cannot be left to reach the renderer).

    Unknown keys pass through untouched. Order of insertion is preserved;
    on a collision (tp and tensor_parallel_size both given) the later
    spelling wins, and the note says so.
    """
    aliases = _alias_map(engine)
    specs = _spec_map(engine)
    out: dict[str, Any] = {}
    notes: list[str] = []
    for key, value in args.items():
        canonical = aliases.get(key, key)
        if canonical != key:
            notes.append(f"{key} → {canonical}")
        if canonical.startswith("no_") and value is True:
            target = aliases.get(canonical[3:], canonical[3:])
            spec = specs.get(target)
            if spec is not None and spec.kind == "switch":
                notes.append(f"--{canonical.replace('_', '-')} → {target}: false")
                canonical, value = target, False
        spec = specs.get(canonical)
        if spec is not None and spec.kind == "switch" and isinstance(value, str):
            word = value.strip().lower()
            if word in _TRUE_WORDS:
                value = True
            elif word in _FALSE_WORDS:
                value = False
        if canonical in out:
            notes.append(f"{canonical} given twice — the later value wins")
        out[canonical] = value
    return out, notes


def catalog_payload() -> dict[str, Any]:
    """The catalog as the UI consumes it: per engine, each flag's canonical
    key, kind, accepted aliases and CLI spelling."""
    return {
        engine: [
            {
                "key": spec.key,
                "kind": spec.kind,
                "aliases": list(spec.aliases),
                "flag": spec.flag or _kebab(spec.key),
            }
            for spec in specs
        ]
        for engine, specs in SPECS.items()
    }


# The plain name sets, kept for callers that only need membership.
KNOWN_FLAGS: dict[str, frozenset[str]] = {
    engine: known_flags(engine) for engine in SPECS
}
