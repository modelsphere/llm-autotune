"""sglang: `python3 -m sglang.launch_server`."""

from typing import TYPE_CHECKING

from app.control.engines.base import MODEL_MOUNT, EngineAdapter, render_args

if TYPE_CHECKING:  # pragma: no cover
    from app.control.launch.base import LaunchSpec


class SGLangAdapter(EngineAdapter):
    name = "sglang"

    def build_command(self, spec: "LaunchSpec") -> list[str]:
        argv = [
            "python3", "-m", "sglang.launch_server",
            "--model-path", MODEL_MOUNT,
            "--served-model-name", spec.served_model_name,
            "--host", "0.0.0.0",
            "--port", str(spec.port),
            *render_args(spec.engine_args, engine=self.name),
        ]
        if spec.nnodes > 1:
            # Placement the platform owns, appended LAST so it wins over a search
            # space that happens to carry the same keys: how many boxes there are
            # and which one this is are facts about the run, not knobs to search.
            # sglang's argparse takes the last occurrence of a repeated flag.
            argv += [
                "--nnodes", str(spec.nnodes),
                "--node-rank", str(spec.rank),
                "--dist-init-addr", spec.dist_init_addr,
            ]
        return argv
