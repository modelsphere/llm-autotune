"""Today's campaign fields as a recipe.

Before recipes, a configuration was spread over a campaign's columns (engine,
image, served name, extra env and volumes) and a flat dict of flags. This is
the one conversion from that shape, used by the LLMService driver until a
campaign stores its recipe, and by the data migration that makes it do so.

It writes what the run would have had, in the chart's spelling:

- flags → `extraArgs` (`--flag=value`, sorted, False switches as the engine's
  `--no-x` where it has one); the context length → `model.contextLength`;
  flags the chart renders itself are left to it;
- the launch drivers' default env, then the campaign's, then the config's
  `env.<NAME>` keys → `env`;
- the campaign's extra volumes (host paths) → `volumes`/`volumeMounts`;
- the weights mounted at /model, where the old drivers put them, so a flag
  that reads a file out of the weights (`--chat-template=/model/...`) still
  finds it;
- cards from the flags' parallelism → `model.gpus`, per node for a gang.
"""

from __future__ import annotations

from typing import Any

from app.control.engines.base import MODEL_MOUNT
from app.control.engines.env_keys import split_env
from app.control.engines.flags import normalize_args
from app.control.recipe import args as argv
from app.control.recipe.check import CHART_FLAGS, CONTEXT_FLAGS
from app.control.recipe.model import ChartRef, Recipe
from app.control.search.validation import cards_used

# The env both launch drivers set under a config's own (ssh_docker/k8s
# DEFAULT_ENV); the chart sets neither, so a recipe carries them.
RUNTIME_ENV = {
    "NVIDIA_DISABLE_REQUIRE": "1",
    "PYTORCH_ALLOC_CONF": "expandable_segments:True",
}

# The oldest chart release a catalog variant may name, per engine (the
# catalog's schema/crds/config.json; vendored in catalog_export/schema).
CHART_MINIMUMS = {"sglang": "0.8.0", "vllm": "0.5.0"}


def chart_range(engine: str) -> str:
    return f">={CHART_MINIMUMS.get(engine, '0.0.0')}"


def split_image(image: str) -> tuple[str, str, str]:
    """repository, tag, digest from `repo[:tag][@sha256:...]`. The tag
    separator is the last colon after the last slash, so a registry port
    survives."""
    ref, _, digest = (image or "").partition("@")
    slash, colon = ref.rfind("/"), ref.rfind(":")
    if colon > slash:
        return ref[:colon], ref[colon + 1 :], digest
    return ref, "", digest


def _volume_name(index: int) -> str:
    return f"extra-{index}"


def from_fields(
    *,
    engine: str,
    image: str,
    served_model_name: str,
    engine_args: dict[str, Any],
    extra_env: dict[str, str] | None = None,
    extra_volumes: dict[str, str] | None = None,
    nodes: int = 1,
    chart_version: str = "",
) -> Recipe:
    flags, config_env = split_env(dict(engine_args or {}))
    flags, _ = normalize_args(engine, flags)
    context = ""
    kept: dict[str, Any] = {}
    for key in sorted(flags):
        value = flags[key]
        if key in CONTEXT_FLAGS:
            context = argv.fmt(value)
        elif key in CHART_FLAGS or value is None or value == "":
            continue
        else:
            kept[key] = value
    nodes = max(1, int(nodes or 1))
    total = max(1, cards_used(flags))
    per_node = total // nodes if total % nodes == 0 else total

    repository, tag, digest = split_image(image)
    values: dict[str, Any] = {
        "image": {"repository": repository, "tag": tag},
        "model": {
            "name": served_model_name,
            "gpus": str(per_node),
            "mountPath": MODEL_MOUNT,
            "contextLength": context,
        },
        "extraArgs": argv.render(engine, kept),
    }
    env = {**RUNTIME_ENV, **(extra_env or {}), **config_env}
    if env:
        values["env"] = [{"name": k, "value": str(v)} for k, v in env.items()]
    if extra_volumes:
        volumes, mounts = [], []
        for index, (host, target) in enumerate(sorted(extra_volumes.items())):
            path, _, mode = str(target).partition(":")
            name = _volume_name(index)
            volumes.append({"name": name, "hostPath": {"path": host}})
            mount: dict[str, Any] = {"name": name, "mountPath": path}
            if mode == "ro":
                mount["readOnly"] = True
            mounts.append(mount)
        values["volumes"] = volumes
        values["volumeMounts"] = mounts
    if nodes > 1:
        values["lws"] = {"enabled": True, "size": nodes}
    return Recipe(
        engine=engine,
        chart=ChartRef(name=engine, version=chart_version or chart_range(engine)),
        values=values,
        image_digest=digest,
    )
