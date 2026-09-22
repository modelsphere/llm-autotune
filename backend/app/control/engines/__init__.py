from app.control.engines.base import (
    MODEL_MOUNT,
    EngineAdapter,
    flag_name,
    render_args,
)
from app.control.engines.sglang import SGLangAdapter
from app.control.engines.vllm import VLLMAdapter

ADAPTER_REGISTRY: dict[str, type[EngineAdapter]] = {
    "sglang": SGLangAdapter,
    "vllm": VLLMAdapter,
}


def get_adapter(engine: str) -> EngineAdapter:
    try:
        return ADAPTER_REGISTRY[engine]()
    except KeyError as exc:
        raise ValueError(
            f"unknown engine '{engine}' (available: {list(ADAPTER_REGISTRY)})"
        ) from exc


__all__ = [
    "ADAPTER_REGISTRY",
    "MODEL_MOUNT",
    "EngineAdapter",
    "SGLangAdapter",
    "VLLMAdapter",
    "flag_name",
    "get_adapter",
    "render_args",
]
