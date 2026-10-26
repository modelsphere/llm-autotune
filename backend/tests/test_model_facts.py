"""A model's facts, from its own config.json: what a search reasons from.

The configs below follow the layouts real families publish (a DeepSeek-style
MoE with MTP layers, a hybrid linear/full attention MoE, a multimodal model
whose language model sits under text_config)."""

from app.control.engines import model_facts as mf
from app.control.launch.base import LaunchDeferred
from app.db.models import ModelConfig
from tests.test_policy_sessions import _boot, night  # noqa: F401  (night is a fixture)

DEEPSEEK_LIKE = {
    "architectures": ["DeepseekV3ForCausalLM"], "model_type": "deepseek_v3",
    "num_hidden_layers": 61, "hidden_size": 7168, "num_attention_heads": 128,
    "num_key_value_heads": 128, "kv_lora_rank": 512, "max_position_embeddings": 163840,
    "n_routed_experts": 256, "num_experts_per_tok": 8, "n_shared_experts": 1,
    "num_nextn_predict_layers": 1, "torch_dtype": "bfloat16",
    "quantization_config": {"quant_method": "fp8", "fmt": "e4m3", "weight_block_size": [128, 128]},
    "rope_scaling": {"type": "yarn", "factor": 40},
}

HYBRID_LIKE = {
    "architectures": ["Qwen3NextForCausalLM"], "model_type": "qwen3_next",
    "num_hidden_layers": 4, "num_key_value_heads": 2, "num_experts": 512,
    "num_experts_per_tok": 10, "mtp_num_hidden_layers": 1, "full_attention_interval": 4,
    "layer_types": ["linear_attention", "linear_attention", "linear_attention", "full_attention"],
    "max_position_embeddings": 262144, "torch_dtype": "bfloat16",
}

MULTIMODAL = {
    "architectures": ["SomeVLForConditionalGeneration"], "model_type": "some_vl",
    "text_config": {"num_hidden_layers": 40, "num_key_value_heads": 8, "num_experts": 0,
                    "max_position_embeddings": 131072},
    "vision_config": {"depth": 32},
}


def test_facts_are_copied_under_one_name_each_never_guessed():
    facts = mf.facts_of(DEEPSEEK_LIKE)
    assert facts["experts"] == 256 and facts["experts_per_token"] == 8
    assert facts["shared_experts"] == 1 and facts["draft_layers"] == 1
    assert facts["kv_lora_rank"] == 512 and facts["max_context"] == 163840
    assert facts["quantization"] == {"quant_method": "fp8", "fmt": "e4m3",
                                     "weight_block_size": [128, 128]}
    assert facts["rope_scaling"] == {"type": "yarn", "factor": 40}

    hybrid = mf.facts_of(HYBRID_LIKE)
    assert hybrid["draft_layers"] == 1  # mtp_num_hidden_layers
    assert hybrid["layer_types"] == {"linear_attention": 3, "full_attention": 1}
    assert hybrid["full_attention_interval"] == 4

    vl = mf.facts_of(MULTIMODAL)
    assert vl["multimodal"] is True and vl["layers"] == 40 and vl["kv_heads"] == 8
    assert "experts" not in vl  # 0 experts is a dense model, not a fact to state
    assert "quantization" not in vl and "draft_layers" not in vl


class _Driver:
    def __init__(self, result):
        self.result, self.calls = result, []

    def run_in_image(self, machine, image, command, timeout, model_path=""):
        self.calls.append((image, command, model_path))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result, "digest"


def test_reading_a_models_config():
    import json

    import pytest

    driver = _Driver(f"noise\n{mf.MARKER}\n{json.dumps(HYBRID_LIKE)}")
    fields = mf.read_model(driver, object(), "sglang:x", "/models/q", 60)
    assert fields["status"] == "ok" and fields["facts"]["experts"] == 512
    assert fields["config"] == HYBRID_LIKE
    assert driver.calls == [("sglang:x", mf.READ_COMMAND, "/models/q")]

    missing = mf.read_model(_Driver(f"{mf.MARKER}\n"), object(), "x", "/models/none", 60)
    assert missing["status"] == "failed" and "empty or missing" in missing["notes"][0]
    with pytest.raises(LaunchDeferred):
        mf.read_model(_Driver(LaunchDeferred("pulling")), object(), "x", "/m", 60)


async def test_the_facts_reach_the_policy(night):  # noqa: F811
    supervisor, driver, http = night
    async with http.db() as session:
        session.add(ModelConfig(model_path="/models/m", status="ok",
                                facts=mf.facts_of(HYBRID_LIKE), config=HYBRID_LIKE, notes=[]))
        await session.commit()
    policy = await _boot(supervisor, driver, http)
    manifest = (await policy.get("/session")).json()
    assert manifest["model"]["facts"]["draft_layers"] == 1
    vocab = (await policy.get("/engine/vocabulary")).json()
    assert vocab["status"] == "none"  # no image read yet; the model's facts are here anyway
    assert vocab["model_facts"]["experts"] == 512
    assert vocab["model_config_json"]["model_type"] == "qwen3_next"
