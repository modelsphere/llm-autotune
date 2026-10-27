"""Environment variables as config keys: `env.<NAME>`.

Some engine switches exist only as environment variables (sglang reads
hundreds of `SGLANG_*`, vllm as many `VLLM_*`), so a config that can only
hold command-line flags cannot reach them. A config names one as
`env.SGLANG_ENABLE_OVERLAP_PLAN_STREAM: true` — still a flat dict of scalars, so it
hashes, compares, sweeps in a search space and deviates like any flag — and
the platform splits those keys off at the launch boundary into the
container's environment, over the campaign's own `extra_env`.

Values render the way both engines read them: a boolean as "1"/"0" (vllm
parses several of its switches with int(), which "true" would crash), any
other scalar as its string.

An env var the engine does not read is silently ignored — the run measures
the config without it and looks like "no effect" — so names are held to a
shape here, and placement and credentials are refused outright: those belong
to the platform and the install, never to a config.
"""

import fnmatch
import re
from typing import Any

from app.control.engines.rules import never

ENV_PREFIX = "env."

_NAME_SHAPE = re.compile(r"^[A-Z_][A-Z0-9_]*$")

# What a config may never set through the environment: where the engine runs
# and who it talks to, the process's own search paths, and credentials
# (labels/common.yaml, `never`), as fnmatch patterns over the variable's name.
PLACEMENT_ENV: tuple[str, ...] = tuple(
    p[len(ENV_PREFIX):] for p in never() if p.startswith(ENV_PREFIX)
)


def is_env_key(key: Any) -> bool:
    return isinstance(key, str) and key.startswith(ENV_PREFIX)


def env_name(key: str) -> str:
    return key[len(ENV_PREFIX):]


def env_value(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def split_env(config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """(engine args, environment) — what goes on the command line, and what
    goes into the container's env, rendered."""
    args: dict[str, Any] = {}
    env: dict[str, str] = {}
    for key, value in (config or {}).items():
        if is_env_key(key):
            env[env_name(key)] = env_value(value)
        else:
            args[key] = value
    return args, env


def placement_env(name: str) -> str | None:
    """The PLACEMENT_ENV pattern `name` matches, or None."""
    return next((p for p in PLACEMENT_ENV if fnmatch.fnmatchcase(name, p)), None)


def env_problems(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Why the config's env keys would be refused — the env half of
    `round_trip_problems`, same {"param", "kind", "message"} shape."""
    problems: list[dict[str, Any]] = []

    def problem(param: str, kind: str, message: str) -> None:
        problems.append({"param": param, "kind": kind, "message": message})

    for key, value in config.items():
        if not is_env_key(key):
            continue
        name = env_name(key)
        if not _NAME_SHAPE.match(name):
            problem(key, "bad_key", f"{name!r} is not an environment variable name (A-Z, 0-9, _)")
        elif (pattern := placement_env(name)) is not None:
            problem(
                key, "placement",
                f"{name} is placement or a credential (matches {pattern!r}), which the "
                "platform and the install set: remove it",
            )
        elif value is None or isinstance(value, (list, tuple, dict)):
            problem(key, "not_renderable", f"{key} must be one string, number or boolean")
        elif isinstance(value, str) and not value.strip():
            problem(key, "not_renderable", f"{key} is an empty string")
    return problems
