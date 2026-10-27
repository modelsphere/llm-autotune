"""What would stop a recipe from launching, or from being published as it is.

Run before a launch is spent, by `/configs/check`, and on the exact document a
publish writes. The rules are the chart's and the catalog's, checked offline:

- site and run values: placement, routing, scaling and naming belong to the
  site's and the run's layers (compose.py) and to a deploy's form in swiss;
  the catalog refuses them in a variant, so a recipe may not carry them;
- chart-owned flags: the chart renders --model-path, --served-model-name,
  --host, --port (and --nnodes, --node-rank, --dist-init-addr under lws/pd)
  itself; a second spelling in extraArgs fights it;
- the chart's own render guards that can be read off the values (pd with
  lws, extraArgs under commandOverride);
- parallelism against the cards the pods claim (shape.py);
- an image with no tag, which would deploy the chart's default engine.

Rendering the chart (`helm template`) catches the rest, where the binary and
the chart are available; see render.py.
"""

from __future__ import annotations

from typing import Any

from app.control.recipe import args as argv
from app.control.recipe.address import BadAddress, is_managed, parse, value_key
from app.control.recipe.model import ROLES, Recipe
from app.control.recipe.shape import parallelism_problems

# Top-level chart values a recipe may not set: the site's, the run's or a
# deploy form's. The same set the catalog's validator refuses in a variant.
SITE_KEYS = frozenset(
    {
        "global",
        "fullnameOverride",
        "serviceId",
        "replicaCount",
        "scaler",
        "modelRoute",
        "cart",
        "sloRequirement",
        "serviceMonitor",
        "continuationGateway",
        "podDisruptionBudget",
        "nodeSelector",
        "tolerations",
        "affinity",
        "schedulerName",
        "priorityClassName",
        "podLabels",
        "cache",
        "strategy",
        "revisionHistoryLimit",
        "namespace",
    }
)
# Deeper paths with the same owner.
# (model.localPath is not among them: catalog entries carry one, and the site
# layer replaces it, as swiss's does.)
SITE_PATHS: tuple[tuple[str, ...], ...] = (
    ("service", "type"),
    ("lws", "replicas"),
    ("pd", "prefill", "affinityReplace"),
    ("pd", "prefill", "schedulerName"),
    ("pd", "decode", "affinityReplace"),
    ("pd", "decode", "schedulerName"),
    ("pd", "router", "affinityReplace"),
    ("pd", "router", "tolerationsReplace"),
)

# Flags the chart renders itself.
CHART_FLAGS = frozenset(
    {
        "model_path",
        "model",
        "served_model_name",
        "host",
        "port",
        "nnodes",
        "node_rank",
        "dist_init_addr",
        "nccl_init_addr",
    }
)
# The chart renders the context length from model.contextLength.
CONTEXT_FLAGS = frozenset({"context_length", "max_model_len"})
# Rendered by the chart too, but harmless to repeat; catalog entries do.
REPEATABLE_CHART_FLAGS = frozenset({"enable_metrics"})

NODEPORT_RANGE = range(30000, 32768)


def problem(kind: str, message: str, param: str = "") -> dict[str, Any]:
    return {"kind": kind, "param": param, "message": message}


def _site_path(path: tuple[str, ...]) -> bool:
    if path and path[0] in SITE_KEYS:
        return True
    return any(path[: len(p)] == p for p in SITE_PATHS)


def _leaf_paths(tree: Any, path: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    if isinstance(tree, dict) and tree:
        out: list[tuple[str, ...]] = []
        for key, child in tree.items():
            out.extend(_leaf_paths(child, (*path, key)))
        return out
    return [path] if path else []


def config_problems(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Problems with the addresses themselves, before anything is applied."""
    out: list[dict[str, Any]] = []
    for key in config:
        try:
            address = parse(key)
        except BadAddress as exc:
            out.append(problem("bad_key", str(exc), key))
            continue
        if address.kind == "value":
            if is_managed(address.path):
                out.append(
                    problem(
                        "bad_key",
                        f"{key}: flags and env are set by their own addresses"
                        " (<flag>, prefill.<flag>, env.<NAME>), not as a whole list",
                        key,
                    )
                )
            elif _site_path(address.path):
                out.append(problem("placement", f"{key} belongs to the site or the run", key))
            elif address.path[0] == "image":
                out.append(problem("placement", "the image is the campaign's", key))
    return out


def recipe_problems(recipe: Recipe) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    values = recipe.values
    image = values.get("image") or {}
    if not image.get("repository") or not str(image.get("tag") or "").strip():
        out.append(
            problem("not_renderable", "the image needs a repository and a tag", "values.image")
        )
    lists = [("", values.get("extraArgs"))] + [
        (f"{role}.", recipe.role(role).get("extraArgs")) for role in ROLES
    ]
    for prefix, items in lists:
        for key in argv.flags(recipe.engine, items):
            if key in CHART_FLAGS:
                out.append(
                    problem(
                        "placement",
                        f"--{key.replace('_', '-')} is rendered by the chart",
                        prefix + key,
                    )
                )
    if values.get("commandOverride") is not None and values.get("extraArgs"):
        out.append(
            problem(
                "not_renderable",
                "extraArgs reach nothing under commandOverride",
                "values.extraArgs",
            )
        )
    if (values.get("pd") or {}).get("enabled") and (values.get("lws") or {}).get("enabled"):
        out.append(problem("not_renderable", "pd and lws are exclusive", "values.lws.enabled"))
    if values.get("commandOverride") is None:
        for message in parallelism_problems(recipe):
            out.append(problem("placement", message))
    return out


def recipe_warnings(recipe: Recipe) -> list[str]:
    """What launches but reads oddly. Site and run values are here rather than
    among the problems because published catalog entries carry some (a pd
    entry turns `scaler` off, which the chart requires): the run's own layer
    sets them at launch, and a config may never tune them (config_problems)."""
    out: list[str] = []
    values = recipe.values
    seen: set[str] = set()
    for path in _leaf_paths(values):
        if _site_path(path):
            top = value_key(path[:1] if path[0] in SITE_KEYS else path)
            if top not in seen:
                seen.add(top)
                out.append(f"{top} is the site's or the run's; the launch sets its own")
    lists = [("", values.get("extraArgs"))] + [
        (f"{role}.", recipe.role(role).get("extraArgs")) for role in ROLES
    ]
    for prefix, items in lists:
        for key in argv.flags(recipe.engine, items):
            if key in CONTEXT_FLAGS:
                out.append(f"{prefix}{key} repeats values.model.contextLength; the later flag wins")
    port = int(((recipe.values.get("service") or {}).get("port")) or 30000)
    if recipe.values.get("hostNetwork") and port in NODEPORT_RANGE:
        out.append(
            f"service.port {port} is in the NodePort range and binds on the node itself"
            " (hostNetwork): a NodePort Service on the same number would intercept it"
        )
    for key in argv.flags(recipe.engine, recipe.values.get("extraArgs")):
        if key in REPEATABLE_CHART_FLAGS:
            out.append(f"--{key.replace('_', '-')} is rendered by the chart already")
    return out
