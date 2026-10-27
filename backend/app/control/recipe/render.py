"""Each engine's command line as the chart builds it, for reading.

The charts assemble an engine's argv from its values: their own flags first
(model path, served name, host, port, context length, metrics; the group's
rank flags under lws or a multi-node pd role; the pd transfer wiring), then
HiCache's, then `extraArgs`, then a pd role's own `extraArgs`, the last
occurrence of a repeated flag winning. This mirrors that order so a policy or
a person sees what each engine will actually run. It is a preview: values the
chart reads from the pod's environment are left as the `${...}` the chart
writes, and `helm template` is the authority.
"""

from __future__ import annotations

from typing import Any

from app.control.recipe.model import ROLES, Recipe

DEFAULT_PORTS = {"sglang": 30000, "vllm": 8000}
DEFAULT_MOUNT = "/models"


def default_port(chart: str) -> int:
    return DEFAULT_PORTS.get(chart, 30000)


def _int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _sglang(recipe: Recipe, role: str, port: int) -> list[str]:
    values = recipe.values
    model = values.get("model") or {}
    pd = values.get("pd") or {}
    lws = values.get("lws") or {}
    flags = [
        "sglang",
        "serve",
        f"--model-path={model.get('mountPath') or DEFAULT_MOUNT}",
        f"--served-model-name={model.get('name') or ''}",
        "--host=0.0.0.0",
        f"--port={port}",
    ]
    if model.get("contextLength") not in (None, ""):
        flags.append(f"--context-length={model['contextLength']}")
    flags.append("--enable-metrics")
    spec = recipe.role(role) if role in ROLES else {}
    size = _int(spec.get("size"), 1) if role in ROLES else _int(lws.get("size"), 2)
    multi = (role in ROLES and size > 1) or (role not in ROLES and lws.get("enabled"))
    if multi:
        flags += [
            f"--nnodes={size}",
            "--node-rank=${LWS_WORKER_INDEX}",
            "--dist-init-addr=${LWS_LEADER_ADDRESS}:"
            f"{spec.get('distPort') or lws.get('distPort') or 29500}",
        ]
    if role in ROLES:
        bootstrap = spec.get("bootstrapPort") or (8998 if role == "prefill" else 8999)
        flags += [
            f"--disaggregation-mode={role}",
            f"--disaggregation-transfer-backend={pd.get('transferBackend') or 'mooncake'}",
            f"--disaggregation-bootstrap-port={bootstrap}",
            "--disaggregation-ib-device=${MOONCAKE_IB_DEVICE}",
        ]
    offload = values.get("localKVOffload") or {}
    if (
        offload.get("enabled")
        and (offload.get("backend") or "hicache") == "hicache"
        and role != "decode"
    ):
        flags.append("--enable-hierarchical-cache")
        for key, value in (offload.get("hicache") or {}).items():
            if value is not None:
                flags.append(f"--hicache-{key}={value}")
    flags += [str(item) for item in values.get("extraArgs") or []]
    if role in ROLES:
        flags += [str(item) for item in spec.get("extraArgs") or []]
    return flags


def _vllm(recipe: Recipe, role: str, port: int) -> list[str]:
    values = recipe.values
    model = values.get("model") or {}
    lws = values.get("lws") or {}
    flags = [
        "vllm",
        "serve",
        f"--model={model.get('mountPath') or DEFAULT_MOUNT}",
        f"--served-model-name={model.get('name') or ''}",
        "--host=0.0.0.0",
        f"--port={port}",
    ]
    if values.get("promptTokensDetails", True):
        flags.append("--enable-prompt-tokens-details")
    context = model.get("maxLen", model.get("contextLength"))
    if context not in (None, ""):
        flags.append(f"--max-model-len={context}")
    spec = recipe.role(role) if role in ROLES else {}
    size = _int(spec.get("size"), 1) if role in ROLES else _int(lws.get("size"), 2)
    if (role in ROLES and size > 1) or (role not in ROLES and lws.get("enabled")):
        flags += [
            "--distributed-executor-backend=mp",
            f"--nnodes={size}",
            "--node-rank=${LWS_WORKER_INDEX}",
            "--master-addr=${LWS_LEADER_ADDRESS}",
            f"--master-port={spec.get('distPort') or lws.get('distPort') or 29500}",
        ]
    if role in ROLES:
        flags.append("--kv-transfer-config=<the chart's, for this role>")
    flags += [str(item) for item in values.get("extraArgs") or []]
    if role in ROLES:
        flags += [str(item) for item in spec.get("extraArgs") or []]
    return flags


def chart_commands(recipe: Recipe) -> dict[str, list[str]]:
    """role → argv: "engine" for one pod or an lws group, "prefill" and
    "decode" under pd. Empty under commandOverride, which replaces it all."""
    if recipe.values.get("commandOverride") is not None:
        return {}
    port = _int((recipe.values.get("service") or {}).get("port"), default_port(recipe.chart.name))
    build = _vllm if recipe.chart.name == "vllm" else _sglang
    if recipe.topology == "pd":
        out = {}
        for role in ROLES:
            spec = recipe.role(role)
            role_port = _int(spec.get("httpPort"), 30001 if role == "prefill" else 30002)
            out[role] = build(recipe, role, role_port)
        return out
    return {"engine": build(recipe, "engine", port)}
