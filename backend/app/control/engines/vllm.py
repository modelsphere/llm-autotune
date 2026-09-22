"""vLLM: `vllm serve <model>`."""

from typing import TYPE_CHECKING

from app.control.engines.base import MODEL_MOUNT, EngineAdapter, render_args

if TYPE_CHECKING:  # pragma: no cover
    from app.control.launch.base import LaunchSpec


class VLLMAdapter(EngineAdapter):
    name = "vllm"

    def build_command(self, spec: "LaunchSpec") -> list[str]:
        # vllm takes the model as a positional argument, not a --model-path.
        return [
            "vllm", "serve", MODEL_MOUNT,
            "--served-model-name", spec.served_model_name,
            "--host", "0.0.0.0",
            "--port", str(spec.port),
            *render_args(spec.engine_args, engine=self.name),
        ]
