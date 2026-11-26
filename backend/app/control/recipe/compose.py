"""A recipe, a site and a run, composed into one LLMService.

    catalog   the recipe's values: everything a tuner changes and a catalog
              publishes
    site      what makes it run on this cluster: the weights' host path,
              placement, the image mirror, the resource its cards are
              requested as, cluster-wide plumbing
    autotune  what makes it a tuning run: its name and labels, the endpoint,
              production features off, and a teardown measured in seconds

The layers merge in that order with the operator's rule (maps merge, anything
else replaces, null deletes), so the run's choices win over a catalog
variant's production settings: a 600-second drain and an hour of grace make
sense for a model serving users and are pure waste between two trials. None
of the site's or the run's values are part of the configuration; the same
recipe composed for another site or another run is the same recipe.

The layer names match swiss's where the meaning matches (`catalog`, `site`),
so a tuning run's object reads like a swiss deploy and a published recipe
drops into swiss's catalog layer unchanged.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from app.control.recipe.model import ROLES, Recipe

API_VERSION = "serving.modelsphere.dev/v1alpha1"
KIND = "LLMService"
PLURAL = "llmservices"
GROUP = "serving.modelsphere.dev"
VERSION = "v1alpha1"

RUN_LABEL = "autotune.modelsphere.dev/run"
MANAGED_LABEL = "autotune.modelsphere.dev/managed"
RECIPE_ANNOTATION = "autotune.modelsphere.dev/recipe-hash"

# The production features a tuning run turns off. Each would attach the run
# to something shared (the gateway, the autoscaler, Prometheus, an SLO
# controller) or hold its pods against a drain.
_FEATURES_OFF = (
    "modelRoute",
    "cart",
    "scaler",
    "sloRequirement",
    "serviceMonitor",
    "podDisruptionBudget",
)

# Teardown: the engine is idle when a trial ends, so there is nothing to drain
# and no reason to give it long to exit.
# What the charts render `model.gpus` under when `model.gpuResource` is unset,
# and the first version of each chart that takes the key (an older one refuses
# it in its values schema). swiss's table, so a run and a deploy agree.
DEFAULT_GPU_RESOURCE = "nvidia.com/gpu"
GPU_RESOURCE_SINCE = {"vllm": "0.9.1", "sglang": "0.9.29"}

RUN_GRACE_SECONDS = 60
RUN_DRAIN_SECONDS = 5


def merge(base: dict[str, Any], layer: dict[str, Any] | None) -> dict[str, Any]:
    """The operator's layer merge: maps merge recursively, anything else
    replaces, null deletes. Returns a new document."""
    out = copy.deepcopy(base)
    for key, value in (layer or {}).items():
        if value is None:
            out.pop(key, None)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def effective_values(layers: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for _, values in layers:
        out = merge(out, values)
    return out


@dataclass
class Site:
    """One cluster's half of a launch. Built from the Cluster row (the
    driver), never stored on a recipe."""

    namespace: str
    chart_repo: str
    chart_credentials_secret: str = ""
    # Where the weights are on the node. A recipe names a model, never a path.
    model_local_path: str = ""
    model_host_path_type: str = ""
    # The site's engine-image mirror ("" = images as written), with swiss's
    # rewrite rule (mirror_image).
    image_mirror: str = ""
    node_selector: dict[str, str] = field(default_factory=dict)
    tolerations: list[dict[str, Any]] = field(default_factory=list)
    scheduler_name: str = ""
    priority_class: str = ""
    # The extended resource this cluster's cards are requested as (its
    # "request cards as", else its GPU resource). A recipe names how many
    # cards, never which resource: the same Ascend recipe asks for
    # huawei.com/Ascend910B4-1 on one site and volcano.sh/Ascend910B4-1 on
    # another.
    gpu_resource: str = ""
    # Raw chart values the cluster needs on every engine (resources a quota
    # demands, shm, securityContext...). Merged last within the site layer.
    extra_values: dict[str, Any] = field(default_factory=dict)
    # "dns" (default: the benchmark runs in this cluster and dials the
    # Service by name) or "nodeport" (opt-in: from outside, via a node).
    endpoint: str = "dns"


@dataclass
class RunPlan:
    run_id: int
    release: str
    # Hostnames the pods may land on (Campaign.node_names). Empty = anywhere
    # the site allows.
    allowed_nodes: list[str] = field(default_factory=list)
    # The campaign's startup budget; 0 keeps the recipe's own startupProbe.
    startup_minutes: int = 0
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)
    # (1.9) Files mounted into the engine container: {container path: key in
    # `files_configmap`}, which the driver creates next to the LLMService.
    files: dict[str, str] = field(default_factory=dict)
    files_configmap: str = ""


FILES_VOLUME = "autotune-files"


def files_keys(files: dict[str, Any]) -> dict[str, str]:
    """{path: ConfigMap key}: `f0`, `f1`... in path order (a path is no key)."""
    return {path: f"f{index}" for index, path in enumerate(sorted(files))}


def files_layer(base: dict[str, Any], run: RunPlan) -> dict[str, Any]:
    """The chart's volumes and mounts with the run's files added. Lists replace
    in a layer merge, so the recipe's and the site's own are carried over."""
    if not run.files:
        return {}
    volumes = list(base.get("volumes") or [])
    mounts = list(base.get("volumeMounts") or [])
    volumes.append({"name": FILES_VOLUME, "configMap": {"name": run.files_configmap}})
    for path, key in sorted(run.files.items()):
        mounts.append({"name": FILES_VOLUME, "mountPath": path, "subPath": key, "readOnly": True})
    return {"volumes": volumes, "volumeMounts": mounts}


def mirror_image(repository: str, mirror: str) -> str:
    """`lmsysorg/sglang` under `registry.example/team` → `registry.example/team/sglang`.

    swiss's rule (site.Profile.MirrorImage): the mirror plus the image's last
    path component. Engine images reach a site's mirror for swiss's deploys,
    so a tuning run pulls exactly what a production deploy of the same recipe
    would. A repository already under the mirror is left alone."""
    if not mirror or repository.startswith(mirror.rstrip("/") + "/"):
        return repository
    return f"{mirror.rstrip('/')}/{repository.rsplit('/', 1)[-1]}"


def get_value(values: dict[str, Any], key: str) -> Any:
    """The value at a dotted key (`a.b.c`, as helm --set spells it); None when
    it is not set."""
    node: Any = values
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _set_value(values: dict[str, Any], key: str, value: Any) -> None:
    node = values
    *parents, leaf = key.split(".")
    for part in parents:
        child = node.get(part)
        if not isinstance(child, dict):
            child = node[part] = {}
        node = child
    node[leaf] = value


def _requests_cards(recipe: Recipe) -> bool:
    """Whether the chart renders a card request from this recipe: model.gpus,
    or a pd role's own. A recipe that names its cards under `resources`
    instead (model.gpus 0) asks the chart for none."""

    def count(raw: Any) -> int:
        try:
            return int(str(raw).strip() or 0)
        except ValueError:
            return 0

    return recipe.gpus > 0 or any(count(recipe.role(role).get("gpus")) > 0 for role in ROLES)


def site_layer(recipe: Recipe, site: Site) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if site.model_local_path:
        out["model"] = {"localPath": site.model_local_path}
        if site.model_host_path_type:
            out["model"]["hostPathType"] = site.model_host_path_type
    if (
        site.gpu_resource
        and site.gpu_resource != DEFAULT_GPU_RESOURCE
        and _requests_cards(recipe)
        and not get_value(recipe.values, "model.gpuResource")
    ):
        # Only when it is not the charts' default, so a recipe on NVIDIA cards
        # renders as it did before the key existed, on any chart version. A
        # recipe that names the resource itself keeps its own.
        out.setdefault("model", {})["gpuResource"] = site.gpu_resource
    repository = ((recipe.values.get("image") or {}).get("repository")) or ""
    mirrored = mirror_image(repository, site.image_mirror)
    if repository and mirrored != repository:
        out["image"] = {"repository": mirrored}
    if site.node_selector:
        out["nodeSelector"] = dict(site.node_selector)
    if site.tolerations:
        out["tolerations"] = copy.deepcopy(site.tolerations)
    if site.scheduler_name:
        out["schedulerName"] = site.scheduler_name
    if site.priority_class:
        out["priorityClassName"] = site.priority_class
    for item in recipe.inputs:
        # An input's answer is the site's (extra_values, merged below), else
        # the input's own default, which goes over whatever base the recipe's
        # values give the key. One with neither is left for unanswered_inputs.
        key, default = str(item.get("key") or ""), item.get("default")
        if key and default is not None and get_value(site.extra_values, key) is None:
            _set_value(out, key, copy.deepcopy(default))
    return merge(out, site.extra_values)


def unanswered_inputs(recipe: Recipe, values: dict[str, Any]) -> list[str]:
    """The recipe's inputs the composed values leave unset, as `key (title)`.
    An answer from any layer counts, the recipe's own values included."""
    out = []
    for item in recipe.inputs:
        key = str(item.get("key") or "")
        if key and get_value(values, key) is None:
            title = str(item.get("title") or "").strip()
            out.append(f"{key} ({title})" if title else key)
    return out


def run_layer(recipe: Recipe, site: Site, run: RunPlan) -> dict[str, Any]:
    out: dict[str, Any] = {
        "fullnameOverride": run.release,
        "replicaCount": 1,
        "podLabels": {RUN_LABEL: str(run.run_id), MANAGED_LABEL: "true", **run.labels},
        "service": {"type": "NodePort" if site.endpoint == "nodeport" else "ClusterIP"},
        "terminationGracePeriodSeconds": RUN_GRACE_SECONDS,
        "lifecycle": {"preStop": {"drainSeconds": RUN_DRAIN_SECONDS, "endpointSyncSeconds": 0}},
    }
    for feature in _FEATURES_OFF:
        out[feature] = {"enabled": False}
    if recipe.topology == "lws":
        out["lws"] = {"replicas": 1, "workerTerminationGracePeriodSeconds": 30}
    if recipe.topology in ("lws", "pd"):
        # The chart recreates the whole group when one pod's container
        # restarts, deleting the pod that crashed and its log with it: a run
        # then fails with only the survivors' "peer went away". A run is over
        # once its engine crashes, so restart in place and keep the log (the
        # pd roles read the same setting). The chart's schema takes `Default`,
        # LWS's older name for `None`.
        out["lws"] = {**out.get("lws", {}), "restartPolicy": "Default"}
    if run.allowed_nodes:
        out["affinity"] = {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [
                        {
                            "matchExpressions": [
                                {
                                    "key": "kubernetes.io/hostname",
                                    "operator": "In",
                                    "values": list(run.allowed_nodes),
                                }
                            ]
                        }
                    ]
                }
            }
        }
    if run.startup_minutes:
        probe = copy.deepcopy(recipe.values.get("startupProbe") or {})
        period = int(probe.get("periodSeconds") or 30)
        out["startupProbe"] = {
            "periodSeconds": period,
            "failureThreshold": max(1, -(-run.startup_minutes * 60 // period)),
        }
    return out


def layers(recipe: Recipe, site: Site, run: RunPlan) -> list[tuple[str, dict[str, Any]]]:
    out = [
        ("catalog", copy.deepcopy(recipe.values)),
        ("site", site_layer(recipe, site)),
    ]
    files = files_layer(effective_values(out), run)
    return [*out, ("autotune", merge(run_layer(recipe, site, run), files))]


def llmservice(recipe: Recipe, site: Site, run: RunPlan, chart_version: str = "") -> dict[str, Any]:
    """The LLMService object for one run. `chart_version` is the version the
    campaign pinned (resolved once, so every trial renders the same chart);
    empty uses the recipe's own, range or not."""
    chart: dict[str, Any] = {
        "name": recipe.chart.name,
        "repo": site.chart_repo,
        "version": chart_version or recipe.chart.version,
    }
    if site.chart_credentials_secret:
        chart["credentialsRef"] = {"name": site.chart_credentials_secret}
    model = {"name": recipe.served_name, "engine": recipe.engine}
    return {
        "apiVersion": API_VERSION,
        "kind": KIND,
        "metadata": {
            "name": run.release,
            "namespace": site.namespace,
            "labels": {RUN_LABEL: str(run.run_id), MANAGED_LABEL: "true", **run.labels},
            "annotations": {RECIPE_ANNOTATION: recipe.hash(), **run.annotations},
        },
        "spec": {
            "chart": chart,
            "layers": [
                {"name": name, "values": values}
                for name, values in layers(recipe, site, run)
                if values
            ],
            "model": {k: v for k, v in model.items() if v},
        },
    }


def service_name(recipe: Recipe, release: str) -> str:
    """The chart's front-door Service: `<fullname>`, or `<fullname>-leader`
    under lws (the LWS controller takes `<fullname>` for its own). Under pd
    the same name selects the routers, so the front door needs nothing more."""
    name = f"{release}-leader" if recipe.topology == "lws" else release
    return name[:63].rstrip("-")


def service_port(recipe: Recipe) -> int:
    """The front door's port: service.port, else the chart's own default
    (sglang 30000, vllm 8000)."""
    from app.control.recipe.render import default_port

    port = (recipe.values.get("service") or {}).get("port")
    return int(port) if port else default_port(recipe.chart.name)
