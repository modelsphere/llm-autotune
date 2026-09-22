"""The inverse of the engine adapters: an engine's config dict from its command.

The adapters (`app.control.engines`) turn a config into a launch command.
Capturing what production runs, and checking how a campaign's config differs
from it, both need the reverse. Kept here — one parser, one placement-flag set —
so the baseline's config is reconstructed the same way everywhere rather than by
a second hand-rolled scan in the driver.
"""

import json
import re
import shlex
from typing import Any

from app.control.engines.flags import normalize_args, short_flags

# A pasted deploy script carries backslash line-continuations — often with
# stray whitespace AFTER the backslash (a copy from a wiki or terminal). shlex
# then reads `\ ` as an escaped space and emits a bare " " token, which the
# docker walk would take for the image and everything after it — the real
# image, the -v mounts, the -e envs — silently drifts into the engine scan.
# Collapse every continuation, trailing whitespace and all, before tokenizing.
_LINE_CONTINUATION = re.compile(r"\\[ \t]*\r?\n")


def _tokenize(text: str) -> list[str]:
    tokens = shlex.split(_LINE_CONTINUATION.sub(" ", text.strip()))
    return [t for t in (token.strip() for token in tokens) if t]

# Set per run by the platform, or meaningless to compare between configs: where
# the model is, who serves it, which socket and which cards it landed on. These
# are stripped from a baseline's config so what remains is the tuning knobs, in
# the same vocabulary a search-space config uses.
PLACEMENT_FLAGS = frozenset(
    {
        "model_path",
        "model",
        "served_model_name",
        "host",
        "port",
        "nccl_port",
        "dist_init_addr",
        "nccl_init_addr",  # sglang's alias spelling of dist_init_addr
        "node_rank",
        "nnodes",
    }
)


def _flag_to_key(flag: str) -> str:
    return flag.lstrip("-").replace("-", "_")


def parse_engine_args(command: Any) -> dict[str, Any]:
    """Engine arguments from a captured container command.

    Accepts the inspected argv list, a JSON-encoded list, or a whole
    `docker run …` line — capture has stored all three shapes over time.
    """
    if isinstance(command, str):
        text = command.strip()
        if text.startswith("["):
            try:
                command = json.loads(text)
            except ValueError:
                command = _tokenize(text)
        else:
            command = _tokenize(text)
    if not isinstance(command, list):
        return {}

    # A pasted deploy script arrives with backslash line-continuations; shlex
    # escapes the newline into a literal "\n" that either becomes its own
    # token (read as a flag's value, turning every bare switch into
    # --flag="\n") or, when the next line starts unindented, glues onto the
    # following flag ("\n--tokenizer-worker-num") and hides it entirely.
    tokens = [t for t in (str(t).strip() for t in command) if t]
    # Everything before the launcher is docker's business, not the engine's.
    # Two launcher spellings exist: the module (`python -m sglang.launch_server`,
    # vllm's `api_server`) and the newer CLI (`sglang serve`), which production
    # deploy scripts now use.
    for index, token in enumerate(tokens):
        if token.endswith("launch_server") or token.endswith("api_server"):
            tokens = tokens[index + 1 :]
            break
        if token == "serve" and index and tokens[index - 1].rsplit("/", 1)[-1] == "sglang":
            tokens = tokens[index + 1 :]
            break

    args: dict[str, Any] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("--"):
            index += 1
            continue
        if "=" in token:
            # The `--flag=value` spelling (vllm serve commands favour it). Split
            # on the first "=" only: the value may itself contain one
            # (--lora-modules name=path).
            flag, _, value = token.partition("=")
            args[_flag_to_key(flag)] = value
            index += 1
            continue
        key = _flag_to_key(token)
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        if following is None or following.startswith("--"):
            args[key] = True  # a bare switch, e.g. --enable-cache-report
            index += 1
        else:
            args[key] = following
            index += 2
    return args


def baseline_engine_args(command: Any) -> dict[str, Any]:
    """Production's engine config with placement stripped — the tuning knobs
    only, so it reads and compares exactly like a search-space config."""
    return {
        key: value
        for key, value in parse_engine_args(command).items()
        if key not in PLACEMENT_FLAGS
    }


def engine_of_command(command: Any) -> str:
    """Which engine a captured command launches — "sglang", "vllm", or "" when
    it cannot be told. Read from the launcher the command names, so a captured
    service can be filed under the same engine a campaign declares."""
    text = (command if isinstance(command, str) else json.dumps(command or [])).lower()
    if "sglang" in text:
        return "sglang"
    if "vllm" in text:
        return "vllm"
    return ""


# ---------------------------------------------------------------------------
# Whole-deployment parsing: a pasted `docker run … image sglang serve …` line
# carries THREE layers — docker flags, the image, and the engine command. The
# docker layer is never stored: env and volumes become the structured fields
# every substrate understands, and the flags the platform sets itself per
# substrate (--gpus, --network, --ipc, ulimits, --name…) are recognized and
# dropped LOUDLY, so nothing silently fails to transfer to k8s.
# ---------------------------------------------------------------------------

# docker-run options that consume the next token as their value (unless
# written --opt=value). Everything else that starts with "-" is boolean.
_DOCKER_VALUE_OPTS = frozenset({
    "-e", "--env", "-v", "--volume", "--mount", "--name", "--network", "--net",
    "--gpus", "--ulimit", "--entrypoint", "-p", "--publish", "--expose",
    "--restart", "--runtime", "--shm-size", "--security-opt", "--cap-add",
    "--cap-drop", "--device", "--label", "-l", "-w", "--workdir", "-u",
    "--user", "--ipc", "--pid", "--uts", "--cpus", "--cpuset-cpus", "-m",
    "--memory", "--memory-swap", "--hostname", "-h", "--add-host", "--dns",
    "--env-file", "--log-driver", "--log-opt", "--stop-signal",
    "--stop-timeout", "--health-cmd", "--health-interval", "--health-retries",
    "--health-timeout",
})
_DOCKER_BOOL_OPTS = frozenset({
    "-d", "--detach", "--rm", "-i", "-t", "-it", "-ti", "--interactive",
    "--tty", "--privileged", "--init", "--no-healthcheck", "--read-only",
    "-P", "--publish-all",
})
# Set by the platform itself, per substrate — the ssh driver renders its own
# --gpus/--network/--ipc/ulimits/--name, the k8s driver renders a pod spec.
# A pasted value for one of these is reported, never stored.
_PLATFORM_MANAGED_OPTS = frozenset({
    "--gpus", "--network", "--net", "--ipc", "--ulimit", "--name", "-d",
    "--detach", "--restart", "--runtime", "--security-opt", "--cap-add",
    "--rm", "-p", "--publish", "--entrypoint",
})


def parse_launch_command(command: Any) -> dict[str, Any]:
    """A pasted deployment — a whole `docker run` line, or a bare serve
    command — compiled into the structured fields a submission carries.

    Returns: engine, image, served_model_name, model_path, service_port,
    engine_args (tuning knobs only), extra_env, extra_volumes,
    platform_managed (docker flags recognized and dropped, as pasted), and
    warnings (anything that could NOT be carried over — the honest list).
    """
    if isinstance(command, str) and command.strip().startswith("["):
        # The inspected argv, JSON-encoded — the shape capture stores.
        try:
            command = json.loads(command.strip())
        except ValueError:
            pass
    if isinstance(command, str):
        tokens = _tokenize(command)
    elif isinstance(command, list):
        tokens = [str(t).strip() for t in command if str(t).strip()]
    else:
        tokens = []

    out: dict[str, Any] = {
        "engine": engine_of_command(command),
        "image": "",
        "served_model_name": "",
        "model_path": "",
        "service_port": 0,
        "engine_args": {},
        "extra_env": {},
        "extra_volumes": {},
        "platform_managed": [],
        "warnings": [],
        "normalized": [],
    }

    engine_tokens = tokens
    if "docker" in tokens[:2] and "run" in tokens[:3]:
        engine_tokens = _split_docker_run(tokens, out)

    # vllm's newer CLI takes the model as a positional the flag parser skips.
    if len(engine_tokens) >= 3 and engine_tokens[0].rsplit("/", 1)[-1] == "vllm" \
            and engine_tokens[1] == "serve" and not engine_tokens[2].startswith("-"):
        out["model_path"] = engine_tokens[2]

    # Single-dash short forms (vllm's `-tp 2`, `-q fp8`) spelled out before the
    # flag walk, which only reads `--` tokens — a bare `-tp` would otherwise
    # vanish without a trace.
    shorts = short_flags(out["engine"])
    if shorts:
        engine_tokens = [
            "--" + shorts[t].replace("_", "-") if t in shorts else t
            for t in engine_tokens
        ]

    args = parse_engine_args(engine_tokens)
    out["served_model_name"] = str(args.get("served_model_name") or "")
    try:
        out["service_port"] = int(args.get("port") or 0)
    except (TypeError, ValueError):
        out["service_port"] = 0
    if 30000 <= out["service_port"] <= 32767:
        out["warnings"].append(
            f"port {out['service_port']} sits in the kube-proxy NodePort range "
            "(30000-32767) and is unreachable on k8s nodes; the platform default "
            "28200 is safer"
        )
    model_path = str(args.get("model_path") or args.get("model") or out["model_path"])

    # The engine sees the CONTAINER path; the host path is on the -v that put
    # it there. Resolve through the mount and drop it from the extras — the
    # platform mounts the weights at /model itself.
    resolved = False
    for host, target in list(out["extra_volumes"].items()):
        if target.split(":", 1)[0].rstrip("/") == model_path.rstrip("/") and model_path:
            model_path = host
            del out["extra_volumes"][host]
            resolved = True
            break
    if model_path and not resolved and (out["image"] or out["platform_managed"]):
        # A docker run was pasted, so the --model-path names a path inside the
        # container — and nothing in the paste says where it lives on the host.
        out["warnings"].append(
            f"model path {model_path!r} is the path inside the container and no -v in "
            "the paste mounts it — set the weights path on the host machine yourself"
        )
    out["model_path"] = model_path
    # One canonical vocabulary: alias spellings fold (tensor_parallel_size →
    # tp), a pasted --no-x on a known switch becomes x: false, and the notes
    # say exactly what moved — shown in the editor, never silent.
    out["engine_args"], out["normalized"] = normalize_args(
        out["engine"], {k: v for k, v in args.items() if k not in PLACEMENT_FLAGS}
    )
    return out


def _split_docker_run(tokens: list[str], out: dict[str, Any]) -> list[str]:
    """Walk the docker section, filling env/volumes and the drop reports, and
    return the in-container command that follows the image."""
    index = tokens.index("run") + 1
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("-"):
            out["image"] = token
            return tokens[index + 1:]
        opt, eq, inline = token.partition("=")
        if eq:
            value = inline
            index += 1
        elif opt in _DOCKER_VALUE_OPTS and index + 1 < len(tokens):
            value = tokens[index + 1]
            index += 2
        else:
            value = ""
            index += 1
            if opt not in _DOCKER_BOOL_OPTS and opt not in _DOCKER_VALUE_OPTS:
                out["warnings"].append(
                    f"unrecognized docker flag {opt} was read as a switch — check "
                    "the compiled fields if the command looks misparsed"
                )
        if opt in ("-e", "--env"):
            key, has_value, env_value = value.partition("=")
            if not has_value:
                out["warnings"].append(
                    f"env {key} has no value in the paste (docker would inherit "
                    "it from the host); set it explicitly"
                )
            out["extra_env"][key] = env_value
        elif opt in ("-v", "--volume"):
            host, sep, target = value.partition(":")
            if sep:
                out["extra_volumes"][host] = target
            else:
                out["warnings"].append(f"volume {value!r} has no container path; dropped")
        elif opt == "--mount":
            out["warnings"].append(
                f"--mount {value!r} is not carried over; rewrite it as -v host:container"
            )
        elif opt in _PLATFORM_MANAGED_OPTS:
            out["platform_managed"].append(f"{opt} {value}".strip())
        elif opt not in _DOCKER_BOOL_OPTS:
            out["warnings"].append(f"docker flag {opt} {value} is not carried over".strip())
    out["warnings"].append("no image found after the docker flags")
    return []
