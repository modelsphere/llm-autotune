"""What the model is, from its own config.json: the facts a search reasons
from (how many layers and KV heads, whether it is MoE and how many experts
are active, whether it carries its own draft layers for speculative decoding,
whether its attention is hybrid, its precision and context).

Read from the weights where the campaign's runs mount them, through the
machine's driver like the engine vocabulary, and kept per model path. Facts
are copied from the config under one name each; a field the config does not
have is left out rather than guessed. The whole config is kept too, for a
policy that wants to read the rest.
"""

import json
from typing import Any

MARKER = "@@AUTOTUNE_MODEL_CONFIG@@"
# One line after the marker (JSON keeps no raw newlines inside its strings),
# so a log reader that keeps only the last lines still has all of it.
READ_COMMAND = f'printf %s {MARKER}; head -c 400000 "$MODEL_DIR/config.json" | tr -d "\\n"; echo'

# One fact, the spellings different model families use for it.
_SAME: dict[str, tuple[str, ...]] = {
    "layers": ("num_hidden_layers", "n_layers", "num_layers"),
    "hidden_size": ("hidden_size", "d_model"),
    "attention_heads": ("num_attention_heads", "n_heads"),
    "kv_heads": ("num_key_value_heads", "n_kv_heads"),
    "head_dim": ("head_dim",),
    "max_context": ("max_position_embeddings", "max_sequence_length", "seq_length"),
    "vocab_size": ("vocab_size",),
    "experts": ("num_experts", "n_routed_experts", "num_local_experts", "moe_num_experts"),
    "experts_per_token": ("num_experts_per_tok", "moe_topk", "top_k_experts"),
    "shared_experts": ("n_shared_experts", "num_shared_experts"),
    # Built-in draft layers a speculative decoder can draft with (MTP / NEXTN).
    "draft_layers": ("num_nextn_predict_layers", "mtp_num_hidden_layers", "num_mtp_layers"),
    "sliding_window": ("sliding_window",),
    "full_attention_interval": ("full_attention_interval",),
    "kv_lora_rank": ("kv_lora_rank",),  # MLA
}


def parse_output(text: str) -> dict[str, Any]:
    if MARKER not in text:
        raise RuntimeError(f"no config.json read: {text.strip()[-500:]}")
    after = text.split(MARKER, 1)[1].strip()
    body = after.splitlines()[0] if after else ""
    if not body:
        raise RuntimeError("config.json is empty or missing")
    return json.loads(body)


def facts_of(config: dict[str, Any]) -> dict[str, Any]:
    # A multimodal checkpoint keeps the language model's shape under text_config.
    text = {**config, **(config.get("text_config") or {})}
    facts: dict[str, Any] = {}
    if config.get("architectures"):
        facts["architectures"] = list(config["architectures"])
    for key in ("model_type",):
        if text.get(key):
            facts[key] = text[key]
    dtype = text.get("torch_dtype") or text.get("dtype")
    if dtype:
        facts["dtype"] = dtype
    for fact, names in _SAME.items():
        value = next((text[n] for n in names if text.get(n) not in (None, "", 0)), None)
        if value is not None:
            facts[fact] = value
    if "text_config" in config:
        facts["multimodal"] = True
    layer_types = text.get("layer_types")
    if isinstance(layer_types, list) and layer_types:
        counts: dict[str, int] = {}
        for t in layer_types:
            counts[str(t)] = counts.get(str(t), 0) + 1
        facts["layer_types"] = counts
    quant = config.get("quantization_config") or text.get("quantization_config")
    if isinstance(quant, dict):
        facts["quantization"] = {
            k: quant[k] for k in ("quant_method", "fmt", "bits", "weight_block_size") if k in quant
        }
    rope = text.get("rope_scaling")
    if isinstance(rope, dict) and rope:
        facts["rope_scaling"] = {k: rope[k] for k in ("type", "rope_type", "factor") if k in rope}
    return facts


def read_model(driver, machine, image: str, model_path: str, timeout: int) -> dict[str, Any]:
    """The model_configs row's fields. Raises LaunchDeferred when the image is
    not on the machine yet."""
    from app.control.launch.base import LaunchDeferred

    try:
        text, _digest = driver.run_in_image(
            machine, image, READ_COMMAND, timeout, model_path=model_path
        )
        config = parse_output(text)
    except LaunchDeferred:
        raise
    except Exception as exc:  # noqa: BLE001 — recorded as a failed read
        return {"model_path": model_path, "status": "failed", "facts": {}, "config": {},
                "notes": [f"could not read {model_path}/config.json: {str(exc)[:1000]}"]}
    return {"model_path": model_path, "status": "ok", "facts": facts_of(config),
            "config": config, "notes": []}
