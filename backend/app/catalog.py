"""Known engine parameters, so a search space can be built by picking rather
than by remembering flag names.

Keys are OUR canonical form (snake_case); the launch driver renders them as
`--flag-name`. Types drive the editor's input widget and the validation of
grid values. `tunable` marks the knobs actually worth searching — everything
else is usually fixed for a campaign and just clutters a picker.
"""

from typing import Any, Literal

ParamType = Literal["int", "float", "bool", "str", "enum"]


def _p(
    name: str,
    type_: ParamType,
    help_: str,
    *,
    tunable: bool = False,
    choices: list[Any] | None = None,
    minimum: float | None = None,
    maximum: float | None = None,
    example: Any = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "type": type_,
        "help": help_,
        "tunable": tunable,
        "choices": choices,
        "min": minimum,
        "max": maximum,
        "example": example,
    }


SGLANG_PARAMS: list[dict[str, Any]] = [
    # -- co-tenancy ----------------------------------------------------------
    # Only needed when several runs share a machine: two tp>1 engines on one
    # host both bootstrap NCCL, and sglang picks that port itself. Set these if
    # a shared run fails during distributed init.
    _p("nccl_port", "int", "Port for NCCL bootstrap. Set it when two multi-GPU "
       "runs share a host and collide during distributed init."),
    _p("dist_init_addr", "str", "host:port for distributed init. Same reason as "
       "nccl_port — only needed for co-tenant multi-GPU runs."),
    # -- parallelism: the knobs with the biggest effect ----------------------
    _p("tp", "int", "Tensor parallel size — GPUs one replica spans.",
       tunable=True, minimum=1, maximum=16, example=[2, 4]),
    _p("dp", "int", "Data parallel size — independent replicas.",
       tunable=True, minimum=1, maximum=16),
    _p("pp", "int", "Pipeline parallel size (multi-node).", minimum=1, maximum=8),
    _p("ep", "int", "Expert parallel size (MoE models only).",
       tunable=True, minimum=1, maximum=16),
    _p("enable_dp_attention", "bool", "Data-parallel attention; helps some MoE models.",
       tunable=True),

    # -- memory / cache ------------------------------------------------------
    _p("mem_fraction_static", "float",
       "Fraction of VRAM for weights + KV cache. Higher = more cache, less headroom.",
       tunable=True, minimum=0.1, maximum=0.99, example=[0.85, 0.9]),
    _p("context_length", "int", "Max context window served."),
    _p("page_size", "int", "KV cache page size (tokens).",
       tunable=True, choices=[1, 16, 32, 64, 128]),
    _p("enable_hierarchical_cache", "bool", "Tiered KV cache using host DRAM as L2.",
       tunable=True),
    _p("hicache_size", "int", "Host DRAM for L2 cache, GB PER RANK (not per machine).",
       tunable=True),
    _p("radix_eviction_policy", "enum", "Radix cache eviction policy.",
       tunable=True, choices=["lru", "lfu"]),
    _p("disable_radix_cache", "bool", "Turn prefix caching off (usually a regression)."),
    _p("kv_cache_dtype", "enum", "KV cache precision; fp8 roughly doubles cache capacity.",
       tunable=True, choices=["auto", "fp8_e5m2", "fp8_e4m3"]),

    # -- scheduling / batching ----------------------------------------------
    _p("chunked_prefill_size", "int",
       "Tokens per prefill chunk. Trades TTFT against decode interference.",
       tunable=True, example=[8192, 16384, 32768]),
    _p("max_running_requests", "int", "Cap on concurrent running requests.",
       tunable=True),
    _p("max_prefill_tokens", "int", "Token budget for a prefill batch.", tunable=True),
    _p("schedule_policy", "enum", "Request scheduling policy.",
       tunable=True, choices=["fcfs", "lpm", "dfs-weight", "random"]),
    _p("schedule_conservativeness", "float",
       "Lower admits more aggressively; higher is safer under memory pressure.",
       tunable=True, minimum=0.1, maximum=2.0),
    _p("mamba_scheduler_strategy", "str", "Scheduler strategy for Mamba/hybrid models."),

    # -- compute backends ----------------------------------------------------
    _p("attention_backend", "enum", "Attention kernel implementation.",
       tunable=True, choices=["flashinfer", "triton", "torch_native", "fa3"]),
    _p("cuda_graph_max_bs", "int", "Largest batch size captured in a CUDA graph.",
       tunable=True),
    _p("disable_cuda_graph", "bool", "Disable CUDA graphs (slower; use to isolate issues)."),
    _p("enable_torch_compile", "bool", "torch.compile the model; long startup cost.",
       tunable=True),
    _p("torch_compile_max_bs", "int", "Max batch size for torch.compile."),
    _p("disable_custom_all_reduce", "bool", "Fall back to NCCL all-reduce."),
    _p("quantization", "enum", "Weight quantization.",
       tunable=True, choices=["fp8", "awq", "gptq", "w8a8_int8", "modelopt_fp4"]),

    # -- speculative decoding ------------------------------------------------
    _p("speculative_algorithm", "enum", "Speculative decoding algorithm.",
       tunable=True, choices=["EAGLE", "EAGLE3", "NEXTN", "STANDALONE"]),
    _p("speculative_num_steps", "int", "Draft steps per verification.",
       tunable=True, example=[3, 5]),
    _p("speculative_eagle_topk", "int", "Draft top-k per step.", tunable=True),
    _p("speculative_num_draft_tokens", "int", "Draft tokens verified per step.",
       tunable=True),

    # -- usually fixed per campaign -----------------------------------------
    _p("trust_remote_code", "bool", "Allow custom modelling code from the checkpoint."),
    _p("reasoning_parser", "str", "Parser for reasoning-model output."),
    _p("tool_call_parser", "str", "Parser for tool-call output."),
    _p("enable_metrics", "bool", "Expose Prometheus metrics."),
    _p("enable_cache_report", "bool", "Report cache hit rates in responses."),
    _p("stream_response_default_include_usage", "bool", "Include usage in streamed chunks."),
    _p("tokenizer_worker_num", "int", "Tokenizer worker processes."),
]

VLLM_PARAMS: list[dict[str, Any]] = [
    _p("tensor_parallel_size", "int", "Tensor parallel size.",
       tunable=True, minimum=1, maximum=16, example=[2, 4]),
    _p("pipeline_parallel_size", "int", "Pipeline parallel size.", minimum=1, maximum=8),
    _p("data_parallel_size", "int", "Data parallel size.", tunable=True, minimum=1),
    _p("gpu_memory_utilization", "float", "Fraction of VRAM to use.",
       tunable=True, minimum=0.1, maximum=0.99, example=[0.85, 0.9]),
    _p("max_model_len", "int", "Max context window served."),
    _p("max_num_seqs", "int", "Max concurrent sequences.", tunable=True),
    _p("max_num_batched_tokens", "int", "Token budget per batch.",
       tunable=True, example=[8192, 16384]),
    _p("enable_chunked_prefill", "bool", "Chunk prefills to protect decode latency.",
       tunable=True),
    _p("enable_prefix_caching", "bool", "Reuse KV for shared prefixes.", tunable=True),
    _p("block_size", "int", "KV cache block size.", tunable=True, choices=[8, 16, 32]),
    _p("kv_cache_dtype", "enum", "KV cache precision.",
       tunable=True, choices=["auto", "fp8", "fp8_e5m2", "fp8_e4m3"]),
    _p("quantization", "enum", "Weight quantization.",
       tunable=True, choices=["fp8", "awq", "gptq", "compressed-tensors"]),
    _p("scheduling_policy", "enum", "Scheduling policy.", tunable=True,
       choices=["fcfs", "priority"]),
    _p("enforce_eager", "bool", "Disable CUDA graphs."),
    _p("swap_space", "int", "CPU swap space per GPU, GiB."),
    _p("trust_remote_code", "bool", "Allow custom modelling code."),
]

CATALOG: dict[str, list[dict[str, Any]]] = {
    "sglang": SGLANG_PARAMS,
    "vllm": VLLM_PARAMS,
}


def params_for(engine: str) -> list[dict[str, Any]]:
    return CATALOG.get(engine, [])


def lookup(engine: str, name: str) -> dict[str, Any] | None:
    return next((p for p in params_for(engine) if p["name"] == name), None)


def coerce(engine: str, name: str, raw: Any) -> Any:
    """Best-effort conversion of an editor string to the parameter's type.

    A search space typed as strings would render `--tp 2` identically but
    compare and hash differently from the int form, quietly splitting one
    config into two candidates.
    """
    spec = lookup(engine, name)
    if spec is None or raw is None or isinstance(raw, bool):
        return raw
    text = str(raw).strip()
    try:
        if spec["type"] == "int":
            return int(text)
        if spec["type"] == "float":
            return float(text)
        if spec["type"] == "bool":
            return text.lower() in ("1", "true", "yes", "on")
    except ValueError:
        return raw  # let validation surface it rather than silently dropping
    return raw
