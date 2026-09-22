"""How an engine is invoked — engine knowledge, not substrate knowledge.

This used to sit inside the ssh+docker driver, which meant "the way you start
sglang" was owned by "the way you reach a machine". They vary independently:
the k8s driver will render the same argv into a custom resource instead of a
`docker run`, and a new engine has to be addable without touching either
driver.

The split is: an adapter decides the *server command*, a driver decides how
that command gets executed somewhere.
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    # Type-only: importing the launch package at runtime would be a cycle
    # (launch -> ssh_docker -> engines), and it would also invert the intended
    # dependency. An adapter describes how to *invoke* an engine; it has no
    # business knowing that a deployment substrate exists.
    from app.control.launch.base import LaunchSpec

# The model directory is bind-mounted rather than passed by host path, so every
# engine sees the weights at the same place regardless of where they live on
# the machine. Mirrors the production deploy scripts.
MODEL_MOUNT = "/model"


def flag_name(key: str) -> str:
    """Our canonical snake_case parameter name as the engine's CLI flag.

    Search spaces, the catalog and the validator all speak snake_case; the
    `--flag-name` form exists only at this boundary. Keeping the translation
    in one function is what lets a config be hashed, compared and stored
    without ever being a string of shell arguments.
    """
    return "--" + key.replace("_", "-")


def render_args(engine_args: dict[str, Any], engine: str = "") -> list[str]:
    """Engine arguments as argv, sorted so the same config always renders the
    same command — a launch command that reordered itself would look like a
    different deployment every time it was compared against a capture.

    With an engine named, each key renders in that engine's own spelling
    (stored `tp` → sglang `--tp-size`, vllm `--tensor-parallel-size`), and
    alias keys from older stored configs resolve the same way. Unknown keys
    render generically — a new engine flag needs nothing from us.

    A True boolean is a bare switch. A False one renders the engine's
    explicit `--no-<flag>` where one exists (all of vllm's bools, sglang's
    negatable few) and is omitted everywhere else — `--enable-x False` is
    parsed as "enabled" by both engines' argparse, and an invented `--no-x`
    on a store_true flag would crash the launch.
    """
    from app.control.engines.flags import negated_flag, render_flag

    out: list[str] = []
    for key, value in sorted(engine_args.items()):
        flag = render_flag(engine, key) if engine else flag_name(key)
        if isinstance(value, bool):
            if value:
                out.append(flag)
            elif engine:
                negated = negated_flag(engine, key)
                if negated is not None:
                    out.append(negated)
        else:
            out += [flag, str(value)]
    return out


class EngineAdapter(ABC):
    """Turns a LaunchSpec into the command that serves the model."""

    name: str = "base"

    @abstractmethod
    def build_command(self, spec: "LaunchSpec") -> list[str]:
        """Full in-container server command. Element 0 becomes the entrypoint."""
