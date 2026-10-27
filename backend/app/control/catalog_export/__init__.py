"""model-catalog export: measured configs → `catalog.swiss/v2` variant files.

The public model catalog (github.com/modelsphere/model-catalog) describes how
a model should be served, and nothing about where. Each way of serving a model
is one variant file, `models/<name>/<id>.yaml`, edited in place with its own
`version` raised on every change; `models/<name>/metadata.yaml` says what the
model is, which variant to preselect, and what tuning measured. This module
turns N measured LaunchConfigs (a baseline and the config a campaign tuned from
it, or several tuned configs) into one variant file per config, and the
model's metadata.yaml: a new one for a model the catalog does not have, or the
published one with this export's tuning result added.

What the catalog already holds is read from its published index
(catalog_index.py): an exported id it already has becomes the next version of
that variant, or no file at all when nothing changed. Every document is
validated against the catalog's own schemas (vendored under schema/, with the
minimum chart versions in schema/pins.json; re-copy when the catalog changes
them) and the rules its validator adds on top.

What is deliberately left out, because the catalog forbids or cannot use it:
- `model.localPath`, host paths, namespaces, scheduling, replicas: site- and
  deploy-owned layers, refused by swiss. The weights are named by `source.hf`
  in metadata.yaml, never by our model_path.
- Our extra_volumes are host paths, so they are dropped unless asked for --
  except one an engine flag reads from (a draft model), which the config
  cannot start without; the catalog's kimi-k3 entry mounts its draft that way.
- env is what the run had: the launch driver's defaults plus extra_env.
- Placement flags (model path, served name, host, port) are chart-owned; the
  chart renders them from `model.*`. `--context-length` moves to
  `model.contextLength` for the same reason.
- `gpuProduct` needs the exact node label ("NVIDIA-H100-80GB-HBM3"), and the
  platform only keeps the normalized card name, so it is written only when the
  caller supplies the labels.

Evidence (by how much the optimized config won, on which workloads) goes in
metadata.yaml's `tuning` entry and the variant `description`; which run
measured what is for the pull request description (`provenance`), never a
file.
"""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.control.engine_command import PLACEMENT_FLAGS
from app.control.engines.base import MODEL_MOUNT
from app.control.engines.flags import negated_flag, render_flag
from app.control.launch_config import LaunchConfig
from app.control.recipe import Recipe

from .portability import ENV_ACTIONS, Input, classify_env, plan_mounts

API_VERSION = "catalog.swiss/v2"
SCHEMA_DIR = Path(__file__).parent / "schema"
# The oldest chart release the catalog accepts a new or changed variant on, per
# engine. A variant asks for that or newer, so a chart fix reaches it without a
# new variant version.
CHART_MINIMUMS: dict[str, str] = json.loads((SCHEMA_DIR / "pins.json").read_text())[
    "minimumChartVersions"
]


def chart_range(engine: str) -> str:
    return f">={CHART_MINIMUMS.get(engine, '0.0.0')}"


# Where the sglang chart mounts the weights when an entry does not say
# (values.yaml: model.mountPath). Every path a flag reads must live under it or
# under one of the entry's own volumeMounts.
CHART_MODEL_MOUNT = "/models"
ENGINES = ("sglang", "vllm")

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")
HF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")

# Chart-owned flags never go into extraArgs; context_length moves to
# model.contextLength instead of being dropped.
CHART_OWNED: frozenset[str] = frozenset(PLACEMENT_FLAGS | {"enable_metrics"})
# Every launch carries these; dropping them is expected, not worth a warning.
_ROUTINE_CHART_OWNED = frozenset({"model_path", "model", "served_model_name", "host", "port"})
_CONTEXT_KEYS = ("context_length", "max_model_len")
# Switches every entry of an engine serves with (see _variant).
SERVING_CONTRACT: dict[str, tuple[str, ...]] = {
    "sglang": ("enable_cache_report", "stream_response_default_include_usage"),
}

# nvidia.com/gpu.product values, as the catalog's own entries spell them. The
# platform keeps only the normalized card name ("H100"), so this is the one
# place a card is taught its node label; a card missing here gets no
# gpuProduct and a warning, never a guessed label that matches no node.
GPU_PRODUCTS: dict[str, list[str]] = {
    "A100": ["NVIDIA-A100-SXM4-80GB"],
    "H100": ["NVIDIA-H100-80GB-HBM3"],
    "H800": ["NVIDIA-H800"],
    "B300": ["NVIDIA-B300-SXM6-AC"],
}

# Startup budget: the newest catalog entries give every model 90 minutes
# (180 x 30s). A measured load only ever raises it — the platform's load is a
# warm one (cached image, often cached weights); a cold node is slower.
STARTUP_FLOOR_MINUTES = 90
STARTUP_MEASURED_FACTOR = 3
PROBE_PERIOD_SECONDS = 30

# A tag that reads as a release (v0.5.19, 0.5.15-cu129, v0.5.10.post1). Anything
# else (dev-dsv41, latest, a branch name) is likely to be moved to another build.
_RELEASE_TAG = re.compile(r"^v?\d+\.\d+(\.\d+)?([.-][0-9A-Za-z]+)*$")
_DEV_WORDS = re.compile(r"dev|latest|nightly|test|tmp|wip|snapshot", re.I)

# Top-level chart value keys a catalog entry must not write (swiss refuses
# them on load): the site profile's and the deploy form's layers.
_FOREIGN_VALUE_KEYS = frozenset(
    {
        "namespace",
        "replicaCount",
        "scaler",
        "modelRoute",
        "cart",
        "sloRequirement",
        "affinity",
        "nodeSelector",
        "tolerations",
        "cache",
    }
)


def fmt_value(value: Any) -> str:
    """A knob value as a flag spells it: 2.0 from a float-snapping policy is 2."""
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return str(value)


@dataclass
class VariantSource:
    """One config to publish as a variant, and what we know about it."""

    config: LaunchConfig
    role: str = ""  # baseline | optimized | "" — shapes the id and description
    label: str = ""  # a human name ("Qwen3.5 baseline v2")
    nodes: int = 1
    # {"metric", "value", "benchmark", "dataset_build", "stage", "feasible"}
    evidence: dict[str, Any] = field(default_factory=dict)
    # {"kind": "campaign_run", "campaign_id", "run_id", ...}
    origin: dict[str, Any] = field(default_factory=dict)
    id: str = ""  # explicit variant id; derived when empty
    description: str = ""  # explicit; derived when empty
    gpu_product: list[str] = field(default_factory=list)
    # How long the measured run took from launch to healthy, when known.
    load_seconds: float | None = None
    # sha256 manifest digest of the engine image the run pulled, when known.
    image_digest: str = ""
    # The tag to publish when the run's image was named by digest alone: the
    # catalog needs a tag, and the platform cannot invent one.
    image_tag: str = ""
    # The recipe the run launched (app/control/recipe), when it ran as one. It
    # is published as it is: no value added, removed or rewritten, so what a
    # deploy renders is what was measured. Without one, `config` is converted
    # (the deprecated drivers' runs).
    recipe: Recipe | None = None


@dataclass
class CatalogModel:
    """What the catalog already publishes for a model, from its index."""

    name: str
    metadata: dict[str, Any]
    # metadata.yaml as published, for the comments it carries; "" when only
    # the index's copy of its fields could be read.
    metadata_text: str = ""
    # id → {"version": "1.0.2", "document": the variant file, when it could be read}
    variants: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class CatalogOptions:
    name: str
    served_name: str = ""  # "" → derived from the configs' served_model_name
    chart_version: str = ""  # "" → the engine's minimum, as a range (>=0.8.0)
    default_variant: str = ""  # an id; "" → the published default, else the optimized one
    include_env: bool = True
    include_volumes: bool = False
    startup_minutes: int | None = None  # None → the floor, raised by a measured load
    # The model as the catalog publishes it; None for a model new to it.
    existing: CatalogModel | None = None
    # The perf report's file name, beside metadata.yaml: a tuning entry needs one.
    report_file: str = ""
    # For a model new to the catalog: its metadata.yaml.
    hf: str = ""
    hf_revision: str = ""
    display_name: str = ""
    description: str = ""
    family: str = ""
    tags: list[str] = field(default_factory=list)
    license: str = ""
    homepage: str = ""
    # The model's human name, for the drafted metadata.
    model_label: str = ""
    # Applied to every variant that has none of its own.
    gpu_product: list[str] = field(default_factory=list)
    # Env var name -> recipe | site | secret | platform: what a person settled
    # for a name the rules do not know (portability.py). Always wins.
    env_categories: dict[str, str] = field(default_factory=dict)


@dataclass
class CatalogFile:
    path: str  # models/<name>/...
    kind: str  # variant | metadata | report | validation
    text: str


@dataclass
class ExportedVariant:
    """One source's variant: where it stands against the published catalog."""

    id: str
    version: str
    role: str
    # new: not in the catalog; updated: a new version of a published variant;
    # unchanged: identical to the published version, so no file is written.
    status: str
    path: str
    document: dict[str, Any]
    # Each env var and mount the run had, and what the export did with it.
    inputs: list[Input] = field(default_factory=list)
    # A tuned variant's result in metadata.yaml: recorded, no_report (no saved
    # report on these runs), no_baseline; "" for any other variant.
    tuning: str = ""
    uplift: float | None = None


@dataclass
class CatalogExport:
    name: str
    variants: list[ExportedVariant] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    files: list[CatalogFile] = field(default_factory=list)
    # What someone should look at before publishing.
    warnings: list[str] = field(default_factory=list)
    # What the export did that needs no action, for whoever wants to know.
    details: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Where each variant came from (campaign / run ids, measured numbers).
    # Platform ids have no place in a public file, so this is for the pull
    # request description.
    provenance: list[str] = field(default_factory=list)
    # True when the catalog already has this model.
    existing: bool = False
    # found | new | unreachable: what the catalog's index said about the model.
    catalog: str = "new"
    # A source.hf read off the weights path, offered to confirm, never written.
    hf_suggestion: str = ""
    # Where the perf report came from: "saved:<report id>"; "" when none ships.
    report_source: str = ""

    @property
    def valid(self) -> bool:
        return not self.errors

    def file(self, kind: str) -> CatalogFile | None:
        return next((f for f in self.files if f.kind == kind), None)

    def payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "existing": self.existing,
            "catalog": self.catalog,
            "variants": [
                {
                    "id": v.id,
                    "version": v.version,
                    "role": v.role,
                    "status": v.status,
                    "path": v.path,
                    "inputs": [i.payload() for i in v.inputs],
                    "tuning": v.tuning,
                    "uplift": v.uplift,
                }
                for v in self.variants
            ],
            "metadata": self.metadata,
            "files": [{"path": f.path, "kind": f.kind, "text": f.text} for f in self.files],
            "warnings": list(self.warnings),
            "details": list(self.details),
            "errors": list(self.errors),
            "valid": self.valid,
            "provenance": list(self.provenance),
            "hf_suggestion": self.hf_suggestion,
            "report_source": self.report_source,
        }


# -- small derivations ---------------------------------------------------------


def slug(text: str, limit: int = 63) -> str:
    """A catalog id: lowercase, [a-z0-9._-], starting alphanumeric."""
    out = re.sub(r"[^a-z0-9._-]+", "-", (text or "").lower()).strip("-._")
    return out[:limit].rstrip("-._")


def _id_slug(text: str) -> str:
    """A variant id segment: [a-z0-9-] only (no dots)."""
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def guess_hf(model_path: str) -> str:
    """`org/name` from a weights path like /models/Qwen/Qwen3.5-35B-A3B, when
    its last two segments look like one. A guess — the caller confirms it."""
    parts = [p for p in (model_path or "").rstrip("/").split("/") if p]
    if len(parts) >= 2:
        candidate = f"{parts[-2]}/{parts[-1]}"
        if HF_RE.match(candidate) and parts[-2] not in ("models", "model", "data", "mnt", "root"):
            return candidate
    return ""


def split_image(image: str) -> tuple[str, str, str]:
    """repository, tag, digest from `repo[:tag][@sha256:…]`. The tag separator
    is the last colon after the last slash, so a registry port survives."""
    ref, _, digest = (image or "").partition("@")
    slash = ref.rfind("/")
    colon = ref.rfind(":")
    if colon > slash:
        return ref[:colon], ref[colon + 1 :], digest
    return ref, "", digest


def _int(args: dict[str, Any], keys: tuple[str, ...]) -> int:
    for key in keys:
        if key in args:
            try:
                return max(1, int(float(args[key])))
            except (TypeError, ValueError):
                return 1
    return 1


def parallelism(args: dict[str, Any]) -> str:
    """ "tp8", "tp8-pp2", "tp4-dp4" — the id segment a variant is known by."""
    tp = _int(args, ("tp", "tp_size", "tensor_parallel_size"))
    pp = _int(args, ("pp", "pp_size", "pipeline_parallel_size"))
    dp = _int(args, ("dp", "dp_size", "data_parallel_size"))
    parts = [f"tp{tp}"]
    if pp > 1:
        parts.append(f"pp{pp}")
    if dp > 1:
        parts.append(f"dp{dp}")
    return "-".join(parts)


def extra_args(engine: str, args: dict[str, Any]) -> tuple[list[str], str, list[str]]:
    """Engine args as `--flag=value` items (the catalog's spelling), the
    context length pulled out for model.contextLength, and what was dropped.

    Sorted like render_args, so the same config always renders the same list.
    False switches render the engine's --no-x where one exists and are
    omitted otherwise — the same rule the launch command follows."""
    items: list[str] = []
    dropped: list[str] = []
    context = ""
    for key, value in sorted(args.items()):
        if key in _CONTEXT_KEYS:
            context = fmt_value(value)
            continue
        if key in CHART_OWNED:
            dropped.append(key)
            continue
        flag = render_flag(engine, key)
        if isinstance(value, bool):
            if value:
                items.append(flag)
            else:
                negated = negated_flag(engine, key)
                if negated is not None:
                    items.append(negated)
        elif value is None or value == "":
            continue
        else:
            items.append(f"{flag}={fmt_value(value)}")
    return items, context, dropped


# -- the builder ---------------------------------------------------------------


def _variant_id(src: VariantSource, gpus: int) -> str:
    if src.id:
        return _id_slug(src.id)[:63]
    cfg = src.config
    parts = [cfg.engine, parallelism(cfg.engine_args)]
    if cfg.gpu_type:
        parts.append(cfg.gpu_type)
    if src.nodes > 1:
        parts.append("lws")
    if src.role:
        parts.append(src.role)
    return _id_slug("-".join(parts))[:63] or f"variant-{gpus}"


def _fmt_metric(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:,.4g}" if abs(value) < 1000 else f"{value:,.0f}"
    return str(value)


_WORDS = {1: "one", 2: "two", 4: "four", 8: "eight"}


def features(args: dict[str, Any]) -> list[str]:
    """What a config does beyond its parallelism, in the catalog's words."""
    out = []
    quant = str(args.get("quantization") or "")
    if quant:
        out.append(f"{quant} weights")
    kv = str(args.get("kv_cache_dtype") or "")
    if kv and kv != "auto":
        out.append(f"{kv.split('_')[0]} KV cache")
    spec = str(args.get("speculative_algorithm") or "")
    if spec:
        out.append(f"{spec.upper()} speculative decoding")
    if args.get("enable_hierarchical_cache") is True:
        out.append("hierarchical KV cache")
    if args.get("enable_dp_attention") is True:
        out.append("DP attention")
    return out


def gain_pct(src: VariantSource, reference: VariantSource | None) -> float | None:
    """How much better this variant measured than the baseline one, in percent
    along the objective's own direction, when both were measured on the same
    metric and benchmark."""
    ev = src.evidence or {}
    ref = (reference.evidence or {}) if reference is not None else {}
    if (
        reference is None
        or reference is src
        or not ev.get("metric")
        or ref.get("metric") != ev.get("metric")
        or ref.get("benchmark", "") != ev.get("benchmark", "")
        or not isinstance(ev.get("value"), (int, float))
        or not isinstance(ref.get("value"), (int, float))
        or not ref["value"]
    ):
        return None
    raw = (float(ev["value"]) / float(ref["value"]) - 1.0) * 100.0
    return -raw if ev.get("direction") == "minimize" else raw


def _describe(src: VariantSource, gpus: int, reference: VariantSource | None) -> str:
    """Hardware and features, the way the catalog's entries read ("TP8 with
    DCP8 on one eight-GPU B300 node, fp8 KV cache"), plus the measured gain over
    the baseline variant. No metric keys or benchmark names: the file is public
    and frozen, and those are specific to one installation."""
    if src.description:
        return src.description[:512]
    cfg = src.config
    shape = parallelism(cfg.engine_args).upper().replace("-", " ")
    card = cfg.gpu_type or "GPU"
    count = _WORDS.get(gpus, str(gpus))
    noun = "GPU" if gpus == 1 else "GPUs"
    if src.nodes > 1:
        where = f"{count} {card} {noun} per node, {src.nodes} nodes"
    else:
        where = f"{count} {card} {noun}, single node"
    head = {"baseline": "Baseline", "optimized": "Tuned by LLM AutoTune"}.get(src.role, "")
    body = f"{shape} on {where}"
    extra = features(cfg.engine_args)
    if extra:
        body += ", " + ", ".join(extra)
    text = f"{head}: {body}." if head else f"{body}."
    scenarios = (src.evidence or {}).get("scenarios") or []
    if src.role == "optimized" and scenarios:
        # Every scenario, not only the ranking one: a +89% headline on the long
        # sweep next to +5% on the short one is a different claim.
        parts = [
            f"{pct_text(s['pct'])} ({s['label']})" for s in scenarios if s.get("pct") is not None
        ]
        if parts:
            text += (
                " Normalized throughput within the SLO vs the baseline variant: "
                + ", ".join(parts)
                + "."
            )
        return text[:512]
    gain = gain_pct(src, reference)
    if gain is not None:
        text += f" {gain:+.0f}% on the tuning benchmark vs the baseline variant."
    return text[:512]


def runtime_env() -> dict[str, str]:
    """Env every launch driver sets under a config's own extra_env."""
    from app.control.launch import k8s, ssh_docker

    return {**ssh_docker.DEFAULT_ENV, **k8s.DEFAULT_ENV}


def startup_minutes(src: VariantSource, opts: CatalogOptions) -> int:
    """The startup budget: the caller's, else the catalog's 90-minute floor,
    raised to 3x the measured load when that is longer. Rounded up to 5."""
    if opts.startup_minutes:
        return int(opts.startup_minutes)
    minutes = STARTUP_FLOOR_MINUTES
    if src.load_seconds:
        minutes = max(minutes, math.ceil(src.load_seconds * STARTUP_MEASURED_FACTOR / 60))
    return min(240, 5 * math.ceil(minutes / 5))


def _variant_from_recipe(
    src: VariantSource,
    opts: CatalogOptions,
    reference: VariantSource | None,
    warnings: list[str],
    errors: list[str],
    details: list[str],
) -> dict[str, Any]:
    """The variant a recipe IS. The values are the recipe's to the byte; what is
    added is only what a publisher decides and the run cannot: the id, the
    description, the cards it fits (gpuProduct) and the image digest it pulled."""
    assert src.recipe is not None
    body = src.recipe.to_variant()
    requires = dict(body["requires"])
    vid = _variant_id(src, int(requires.get("gpus") or 0))
    description = _describe(src, requires["gpus"], reference)
    variant: dict[str, Any] = {"id": vid, "description": description}
    variant["engine"] = body["engine"]
    variant["chart"] = {
        "name": body["chart"]["name"],
        "version": opts.chart_version or body["chart"]["version"] or chart_range(body["engine"]),
    }
    image = dict(body.get("image") or {})
    if not image.get("tag") and src.image_tag.strip():
        image["tag"] = src.image_tag.strip()
    digest = image.get("digest") or src.image_digest
    if image.get("repository") and image.get("tag"):
        if re.fullmatch(r"sha256:[a-f0-9]{64}", digest or ""):
            image["digest"] = digest
        tag = image["tag"]
        if (not _RELEASE_TAG.match(tag) or _DEV_WORDS.search(tag)) and "digest" not in image:
            warnings.append(f"{vid}: image tag {tag} isn't a release tag and may move")
        variant["image"] = image
    else:
        errors.append(f"{vid}: the recipe's image has no tag; the catalog names an image by tag")
    products = src.gpu_product or opts.gpu_product or GPU_PRODUCTS.get(src.config.gpu_type, [])
    if products:
        requires["gpuProduct"] = list(dict.fromkeys(products))
    else:
        warnings.append(
            f"{vid}: no node label known for {src.config.gpu_type or 'this card'}: set gpuProduct"
            " under Advanced"
        )
    variant["requires"] = requires
    variant["values"] = body["values"]
    details.append(f"{vid}: the recipe the run measured, published unchanged")
    return variant


def _variant(
    src: VariantSource,
    opts: CatalogOptions,
    reference: VariantSource | None,
    warnings: list[str],
    errors: list[str],
    inputs: list[Input],
    details: list[str],
) -> dict[str, Any]:
    if src.recipe is not None:
        return _variant_from_recipe(src, opts, reference, warnings, errors, details)
    cfg = src.config
    nodes = max(1, int(src.nodes or 1))
    total = max(1, cfg.cards)
    gpus = total // nodes if total % nodes == 0 else total
    if total % nodes:
        warnings.append(f"{total} GPUs don't split evenly over {nodes} nodes")
    vid = _variant_id(src, gpus)

    variant: dict[str, Any] = {"id": vid}
    variant["description"] = _describe(src, gpus, reference)
    variant["engine"] = cfg.engine
    variant["chart"] = {
        "name": cfg.engine,
        "version": opts.chart_version or chart_range(cfg.engine),
    }

    repository, tag, digest = split_image(cfg.image)
    tag = tag or src.image_tag.strip()
    digest = digest or src.image_digest
    if repository and tag:
        variant["image"] = {"repository": repository, "tag": tag}
        if re.fullmatch(r"sha256:[a-f0-9]{64}", digest or ""):
            variant["image"]["digest"] = digest
        if not _RELEASE_TAG.match(tag) or _DEV_WORDS.search(tag):
            if "digest" in variant["image"]:
                details.append(f"{vid}: image tag {tag} isn't a release tag; its digest is pinned")
            else:
                warnings.append(f"{vid}: image tag {tag} isn't a release tag and may move")
    else:
        # Without an image the chart's default engine would deploy, which is
        # not what was measured.
        errors.append(
            f"{vid}: image {cfg.image!r} has no tag, and the catalog names an image by"
            " repository and tag: give the tag this build is published under"
        )

    requires: dict[str, Any] = {"gpus": gpus}
    if nodes > 1:
        requires.update({"nodes": nodes, "topology": "lws", "rdma": True})
    else:
        requires["topology"] = "single-node"
    products = src.gpu_product or opts.gpu_product or GPU_PRODUCTS.get(cfg.gpu_type, [])
    if products:
        requires["gpuProduct"] = list(dict.fromkeys(products))
    else:
        warnings.append(
            f"{vid}: no node label known for {cfg.gpu_type or 'this card'}: set gpuProduct"
            " under Advanced"
        )
    variant["requires"] = requires

    engine_args = dict(cfg.engine_args)
    # The serving contract every catalog entry carries: clients read cached
    # tokens and streamed usage from the response. A config measured
    # without them still measured the same throughput (they only add fields),
    # so they are added rather than leaving an entry that answers differently
    # from every other one. An explicit False is the config's choice and stays.
    missing = [key for key in SERVING_CONTRACT.get(cfg.engine, ()) if key not in engine_args]
    if missing:
        engine_args.update(dict.fromkeys(missing, True))
        details.append(f"{vid}: added {', '.join(missing)}, which every catalog entry serves with")
    args, context, dropped = extra_args(cfg.engine, engine_args)
    dropped = [key for key in dropped if key not in _ROUTINE_CHART_OWNED]
    if dropped:
        details.append(f"{vid}: left to the chart: {', '.join(dropped)}")

    # The chart-0.7.1 shape of the catalog's newest entries (qwen3.6-35b-a3b,
    # kimi-k3, glm5.3): weights preflight, scratch and shm volumes, ephemeral
    # storage, a startup budget sized for a cold load, and a drain on the way
    # out so a scale-down or rollout does not cut requests in flight.
    values: dict[str, Any] = {
        # Never pinned unless the config pinned it: a number here silently caps
        # the model.
        "model": {"hostPathType": "Directory", "contextLength": context},
        "modelCheck": {"enabled": True, "requiredGlobs": ["config.json", "*.safetensors"]},
    }
    # A flag that reads a file out of the weights (--chat-template=/model/...)
    # depends on where the chart mounts them, so the mount is pinned whenever
    # one does, as the catalog's hand-written entries pin it.
    if any(str(v).startswith(MODEL_MOUNT + "/") for v in engine_args.values()):
        values["model"] = {"mountPath": MODEL_MOUNT, **values["model"]}
    if nodes > 1:
        values["lws"] = {"enabled": True, "size": nodes}
    values["extraArgs"] = args
    # The env the run actually had: the launch driver's defaults under the
    # config's own. A config measured without extra_env still ran with the
    # defaults, and an entry that drops them is not what was measured.
    env = {**runtime_env(), **cfg.extra_env}
    # A mount an env var points into is kept only when that env var itself
    # could be: one named for this site's setup takes its files with it.
    readers = {
        k: v
        for k, v in env.items()
        if classify_env(k, "", [], opts.env_categories)[0] in ("recipe", "unknown")
    }
    plan = plan_mounts(
        dict(cfg.extra_volumes),
        dict(cfg.engine_args),
        readers,
        MODEL_MOUNT,
        keep_all=opts.include_volumes,
    )
    provided = [MODEL_MOUNT, "/dev/shm", "/root/.cache", *plan.provided]
    kept_env = []
    for name, value in env.items():
        category, reason = classify_env(name, str(value), provided, opts.env_categories)
        action = ENV_ACTIONS[category] if opts.include_env else "left out"
        if action == "kept":
            kept_env.append({"name": name, "value": str(value)})
        inputs.append(Input("env", name, str(value), category, action, reason))
    if kept_env:
        values["env"] = kept_env
    inputs.extend(plan.inputs)
    if opts.include_volumes and cfg.extra_volumes:
        warnings.append(f"{vid}: host paths are published as they are")

    shm = "32Gi" if gpus >= 4 else "16Gi"
    volumes: list[dict[str, Any]] = [
        *plan.volumes,
        {"name": "dot-cache", "emptyDir": {"sizeLimit": "10Gi"}},
        {"name": "dshm", "emptyDir": {"medium": "Memory", "sizeLimit": shm}},
    ]
    mounts: list[dict[str, Any]] = [
        *plan.mounts,
        {"name": "dot-cache", "mountPath": "/root/.cache"},
        {"name": "dshm", "mountPath": "/dev/shm"},
    ]
    values["volumes"] = volumes
    values["volumeMounts"] = mounts

    storage = "28Gi" if nodes > 1 else "16Gi"
    values["resources"] = {
        "requests": {"ephemeral-storage": storage},
        "limits": {"ephemeral-storage": storage},
    }
    if nodes > 1:
        values["resources"]["limits"]["rdma/hca_shared"] = "1"

    # Cold-load time is a property of the model, which is why probes live in
    # the catalog: a default failureThreshold kills a long load in 45 seconds.
    minutes = startup_minutes(src, opts)
    values["progressDeadlineSeconds"] = minutes * 60 + 1800
    values["startupProbe"] = {
        "httpGet": {"path": "/health", "port": "http"},
        "periodSeconds": PROBE_PERIOD_SECONDS,
        "timeoutSeconds": 10,
        "failureThreshold": math.ceil(minutes * 60 / PROBE_PERIOD_SECONDS),
    }
    values["terminationGracePeriodSeconds"] = 3600
    values["lifecycle"] = {
        "preStop": {"drainSeconds": 600, "pollIntervalSeconds": 5},
        "forceShutdown": True,
        "preStopKill": True,
    }
    values["hangWatcher"] = {"enabled": True}
    variant["values"] = values
    return variant


def build_catalog_entry(sources: list[VariantSource], opts: CatalogOptions) -> CatalogExport:
    """The variant files and metadata.yaml for these configs, validated."""
    warnings: list[str] = []
    details: list[str] = []
    errors: list[str] = []
    if not sources:
        raise ValueError("at least one config is needed")

    existing = opts.existing
    if existing is not None:
        # The catalog's own name wins: a second spelling would be a second model.
        name = existing.name
        if opts.name and slug(opts.name) != name:
            details.append(f"the catalog has this model as {name}")
    else:
        name = opts.name if NAME_RE.match(opts.name or "") else slug(opts.name)
        if not name:
            raise ValueError(f"catalog name {opts.name!r} has nothing usable in it")
        if name != opts.name:
            details.append(f"name {opts.name} written as {name}")

    for src in sources:
        if src.config.engine not in ENGINES:
            errors.append(f"engine {src.config.engine!r} is not one the catalog serves")

    served = opts.served_name or next(
        (s.config.served_model_name for s in sources if s.config.served_model_name), ""
    )
    others = {s.config.served_model_name for s in sources if s.config.served_model_name}
    if len(others) > 1:
        warnings.append(f"the runs serve different names; all variants serve {served}")

    reference = next((s for s in sources if s.role == "baseline"), None)
    inputs: list[list[Input]] = [[] for _ in sources]
    bodies = [
        _variant(s, opts, reference, warnings, errors, ins, details)
        for s, ins in zip(sources, inputs, strict=True)
    ]
    for body, src in zip(bodies, sources, strict=True):
        warnings += judge(body["id"], src)
    seen: dict[str, int] = {}
    for body in bodies:
        base = body["id"]
        seen[base] = seen.get(base, 0) + 1
        if seen[base] > 1:
            body["id"] = f"{base[:60]}-{seen[base]}"

    exported: list[ExportedVariant] = []
    for body, src, ins in zip(bodies, sources, inputs, strict=True):
        doc: dict[str, Any] = {
            "apiVersion": API_VERSION,
            "kind": "Variant",
            "model": name,
            "id": body["id"],
            "version": "1.0.0",
        }
        if served and served != name:
            doc["servedName"] = served
        doc.update({k: v for k, v in body.items() if k != "id"})
        status = "new"
        published = (existing.variants if existing else {}).get(doc["id"])
        if published is not None:
            was = str(published.get("version") or "1.0.0")
            old = published.get("document")
            if old is not None and _same_variant(old, doc):
                doc["version"] = was
                status = "unchanged"
                details.append(f"{doc['id']}: the same as the published {was}")
            else:
                doc["version"] = bump_minor(was)
                status = "updated"
                if old is None:
                    details.append(f"{doc['id']}: the published {was} could not be read to compare")
        errors += [f"{doc['id']}: {e}" for e in validate(doc, "variant")]
        # Our own rules guard the conversion of a deprecated driver's config
        # (no site path leaking into a variant). A recipe was not converted: it
        # meets the catalog's own rules, the schema above, and nothing stricter.
        if src.recipe is None:
            errors += [f"{doc['id']}: {e}" for e in variant_problems(doc)]
        exported.append(
            ExportedVariant(
                id=doc["id"],
                version=doc["version"],
                role=src.role,
                status=status,
                path=f"models/{name}/{doc['id']}.yaml",
                document=doc,
                inputs=ins,
            )
        )

    meta, suggestion = _metadata(name, sources, exported, opts, warnings, errors)
    files = [
        CatalogFile(v.path, "variant", HEADER + dump(v.document))
        for v in exported
        if v.status != "unchanged"
    ]
    if existing is not None and existing.metadata_text and "#" in existing.metadata_text:
        details.append("metadata.yaml is rewritten from its fields, without its comments")
    files.append(CatalogFile(f"models/{name}/metadata.yaml", "metadata", dump(meta)))
    return CatalogExport(
        name=name,
        variants=exported,
        metadata=meta,
        files=files,
        warnings=warnings,
        errors=errors,
        provenance=provenance(sources),
        existing=existing is not None,
        catalog="found" if existing is not None else "new",
        details=details,
        hf_suggestion=suggestion,
    )


def _same_variant(old: dict[str, Any], new: dict[str, Any]) -> bool:
    """The same file but for its version: re-exporting it needs no new one."""
    strip = ("version",)
    return {k: v for k, v in old.items() if k not in strip} == {
        k: v for k, v in new.items() if k not in strip
    }


def _metadata(
    name: str,
    sources: list[VariantSource],
    exported: list[ExportedVariant],
    opts: CatalogOptions,
    warnings: list[str],
    errors: list[str],
) -> tuple[dict[str, Any], str]:
    """metadata.yaml: the published one with this export's results added, or
    a new one. Returns it and a source.hf suggestion (never written)."""
    existing = opts.existing
    suggestion = ""
    if existing is not None:
        meta = copy.deepcopy(existing.metadata)
        if meta.get("apiVersion") != API_VERSION:
            errors.append(
                f"the catalog still describes {name} in the v1 format: it moves to v2"
                " (model-catalog's hack/migrate-v2.js) before v2 variants can be added"
            )
    else:
        hf = opts.hf.strip()
        draft = suggest_metadata(sources, opts.model_label or name)
        meta = {"apiVersion": API_VERSION, "name": name}
        display = opts.display_name or draft["display_name"]
        description = opts.description or draft["description"]
        family = opts.family or draft["family"]
        tags = opts.tags or draft["tags"]
        if display:
            meta["displayName"] = display[:128]
        if description:
            meta["description"] = description[:2048]
        if opts.homepage:
            meta["homepage"] = opts.homepage
        if opts.license:
            meta["license"] = opts.license
        if family:
            meta["family"] = _id_slug(family)
        if tags:
            meta["tags"] = list(dict.fromkeys(_id_slug(t) for t in tags if _id_slug(t)))
        if hf:
            meta["source"] = {"hf": hf}
            if opts.hf_revision:
                meta["source"]["revision"] = opts.hf_revision
        else:
            # Each site finds the weights from this id, so it is asked for,
            # never read off our own weights path.
            suggestion = next(
                (g for g in (guess_hf(s.config.model_path) for s in sources) if g), ""
            )
            errors.append(
                "source.hf: the model is new to the catalog, and every site finds its"
                " weights by its Hugging Face repo id: give it"
                + (f" (the weights path suggests {suggestion})" if suggestion else "")
            )

    ids = {v.id for v in exported} | set(existing.variants if existing else {})
    default = (
        opts.default_variant
        or meta.get("defaultVariant")
        or next((v.id for v in exported if v.role == "optimized"), "")
    )
    if default:
        if default not in ids:
            errors.append(f"default variant {default!r} is not a variant of {name}")
        meta["defaultVariant"] = default

    tuning = _tuning(sources, exported, opts, warnings)
    if tuning:
        recorded = [t for t in meta.get("tuning") or [] if t not in tuning]
        meta["tuning"] = recorded + tuning
    named = re.compile(r"(^|-)optimi[sz]ed(-|$)")
    covered = {str(t.get("optimized", "")).split("@")[0] for t in meta.get("tuning") or []}
    for v in exported:
        # An optimized variant was already warned about by _tuning.
        if named.search(v.id) and v.id not in covered and v.role != "optimized":
            warnings.append(
                f"{v.id}: the id says optimized; the catalog will want its tuning result"
            )
    if "source" in meta:
        errors += [f"metadata: {e}" for e in validate(meta, "metadata.v2")]
    # Key order as the catalog's own files have it.
    order = (
        "apiVersion",
        "name",
        "displayName",
        "description",
        "homepage",
        "license",
        "family",
        "series",
        "quantization",
        "tags",
        "deprecated",
        "defaultVariant",
        "tuning",
        "source",
    )
    return {k: meta[k] for k in order if k in meta} | {
        k: v for k, v in meta.items() if k not in order
    }, suggestion


def _tuning(
    sources: list[VariantSource],
    exported: list[ExportedVariant],
    opts: CatalogOptions,
    warnings: list[str],
) -> list[dict[str, Any]]:
    """One entry per optimized variant: what it gained over the baseline
    exported with it, on the benchmark and on each workload, and the report.
    Without a baseline or a report there is no entry, and the variant says why
    (ExportedVariant.tuning): the catalog's own check asks for one before the
    pull request passes."""
    pairs = list(zip(exported, sources, strict=True))
    base = next(((v, s) for v, s in pairs if v.role == "baseline"), None)
    out = []
    for v, src in pairs:
        if v.role != "optimized":
            continue
        if base is None:
            v.tuning = "no_baseline"
            continue
        gain = gain_pct(src, base[1])
        v.uplift = round_pct(gain) if gain is not None else None
        if not opts.report_file:
            v.tuning = "no_report"
            continue
        v.tuning = "recorded"
        entry: dict[str, Any] = {
            "baseline": f"{base[0].id}@{base[0].version}",
            "optimized": f"{v.id}@{v.version}",
        }
        if v.uplift is not None:
            entry["uplift"] = v.uplift
        workloads: list[dict[str, Any]] = []
        for sc in (src.evidence or {}).get("scenarios") or []:
            label = str(sc.get("label") or sc.get("key") or "")[:64]
            if label and sc.get("pct") is not None and label not in {w["name"] for w in workloads}:
                workloads.append({"name": label, "uplift": round_pct(float(sc["pct"]))})
        if workloads:
            entry["workloads"] = workloads
        entry["report"] = opts.report_file
        out.append(entry)
    return out


def round_pct(pct: float) -> float:
    """Half up to one decimal, as pct_text prints it."""
    rounded = math.floor(abs(pct) * 10 + 0.5 + 1e-6) / 10
    return rounded if pct >= 0 else -rounded


def bump_minor(version: str) -> str:
    major, minor, _ = _version_tuple(version)
    return f"{major}.{minor + 1}.0"


def pct_text(pct: float) -> str:
    """A change rounded the way the perf report prints it (frontend/src/report/
    data.ts pctText: half up to one decimal), so an entry and its report never
    differ in the last digit -- 119.85 is +119.9%, where Python's own float
    formatting gives +119.8%."""
    rounded = math.floor(abs(pct) * 10 + 0.5 + 1e-6) / 10
    sign = "" if rounded == 0 else "+" if pct > 0 else "-"
    return f"{sign}{rounded:.1f}%"


def judge(vid: str, src: VariantSource) -> list[str]:
    """What a reviewer must see before this variant is published as better than
    the baseline: a scenario it does not win, answer quality it loses, or a
    comparison the platform itself calls not comparable."""
    ev = src.evidence or {}
    out = []
    for reason in ev.get("not_comparable") or []:
        out.append(f"{vid}: the baseline and this config are not comparable -- {reason}")
    if src.role != "optimized":
        return out
    for sc in ev.get("scenarios") or []:
        pct = sc.get("pct")
        if pct is not None and pct <= 0:
            out.append(
                f"{vid}: does NOT beat the baseline on {sc['label']} "
                f"({pct_text(pct)} normalized throughput within the SLO)"
            )
    for q in ev.get("quality") or []:
        points = (q["attempt"] - q["baseline"]) * 100
        if points < 0:
            out.append(
                f"{vid}: answer quality drops on {q['name']}: "
                f"{q['baseline']:.2%} -> {q['attempt']:.2%} ({points:+.2f} points)"
            )
    return out


def _version_tuple(v: str) -> tuple[int, int, int]:
    m = VERSION_RE.match(v or "")
    if not m:
        return (0, 0, 0)
    major, minor, patch = (int(x) for x in v.split("-", 1)[0].split("."))
    return (major, minor, patch)


HEADER = "# Serving configs measured, and where marked tuned, by LLM AutoTune.\n"


def provenance(sources: list[VariantSource]) -> list[str]:
    """One line per variant: which campaign and run measured it and what it
    scored. For the pull request description -- never the file."""
    lines = []
    for src in sources:
        origin = src.origin or {}
        ev = src.evidence or {}
        who = src.label or src.role or src.config.engine
        refs = ", ".join(
            f"{k.replace('_id', '')} {origin[k]}"
            for k in ("campaign_id", "run_id")
            if origin.get(k)
        )
        metric = (
            f"; {ev['metric']}={_fmt_metric(ev['value'])}"
            if ev.get("metric") and ev.get("value") is not None
            else ""
        )
        extra = "".join(
            f"; {k}={ev[k]}" for k in ("benchmark", "dataset_build", "stage") if ev.get(k)
        )
        load = f"; loaded in {src.load_seconds / 60:.0f} min" if src.load_seconds else ""
        lines.append(f"{who}: {refs or 'no run on record'}{metric}{extra}{load}")
    return lines


def suggest_metadata(sources: list[VariantSource], model_label: str) -> dict[str, Any]:
    """A first draft of metadata.yaml's cosmetic fields, from what the platform
    knows: the model's name and the configs being published. Every field is
    only a default — the caller's value always wins — and nothing is invented
    that the configs do not show (a reasoning tag needs a reasoning parser)."""
    label = (model_label or "").strip()
    lowered = label.lower()
    family_match = re.match(r"[a-z]+", lowered)
    family = family_match.group(0) if family_match else ""

    tags = ["chat"]
    if re.search(r"(?<![a-z0-9])a\d+(\.\d+)?b(?![a-z0-9])", lowered) or "moe" in lowered:
        tags.append("moe")
    args_all = [s.config.engine_args or {} for s in sources]
    if any(a.get("reasoning_parser") for a in args_all):
        tags.append("reasoning")
    if any(a.get("tool_call_parser") for a in args_all):
        tags.append("tool-use")
    if re.search(r"(?<![a-z])(vl|vision)(?![a-z])", lowered):
        tags.append("vision")
    if any(s.nodes > 1 for s in sources):
        tags.append("multi-node")
    if any(a.get("speculative_algorithm") for a in args_all):
        tags.append("speculative-decoding")
    for quant in ("fp8", "fp4"):
        if any(quant in str(a.get("quantization") or "") for a in args_all):
            tags.append(quant)

    # Metadata is shared by every version, so it describes the model and the
    # hardware it runs on -- never "2 variants: ...", which the next version
    # would make wrong.
    cards = []
    for src in sources:
        cfg = src.config
        noun = "GPU" if cfg.cards == 1 else "GPUs"
        where = f"{cfg.cards} {cfg.gpu_type} {noun}" if cfg.gpu_type else f"{cfg.cards} {noun}"
        if src.nodes > 1:
            where = f"{src.nodes} nodes of {cfg.cards // src.nodes} {cfg.gpu_type or ''} GPUs"
        if where not in cards:
            cards.append(where)
    feats = []
    for src in sources:
        for f in features(src.config.engine_args):
            if f not in feats:
                feats.append(f)
    served = next(
        (src.config.served_model_name for src in sources if src.config.served_model_name), ""
    )
    parts = [
        f"{label or 'This model'} on {' or '.join(cards)}"
        + (f', served as "{served}"' if served and served != label else "")
        + "."
    ]
    if feats:
        parts.append("Configs use " + ", ".join(feats) + ".")
    if any(src.nodes > 1 for src in sources):
        parts.append(
            "Does not fit on one node: a LeaderWorkerSet group, which needs the LWS"
            " controller and RDMA (rdma-shared-dev-plugin)."
        )
    return {
        "display_name": label,
        "description": " ".join(parts),
        "family": family,
        "tags": tags,
    }


# -- serialization and validation ---------------------------------------------


class _Dumper(yaml.SafeDumper):
    """Block style with sequences indented under their key, the way the
    catalog's hand-written entries are."""

    def increase_indent(self, flow: bool = False, indentless: bool = False):
        return super().increase_indent(flow, False)


def dump(document: dict[str, Any]) -> str:
    return yaml.dump(
        document,
        Dumper=_Dumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=4096,
    )


_SCHEMAS: dict[str, dict[str, Any]] = {}


def schema(kind: str) -> dict[str, Any]:
    if kind not in _SCHEMAS:
        _SCHEMAS[kind] = json.loads((SCHEMA_DIR / f"{kind}.schema.json").read_text())
    return _SCHEMAS[kind]


def _registry():
    """Every vendored schema by its $id: the v2 schemas refer to each other."""
    from referencing import Registry, Resource

    docs = [json.loads(p.read_text()) for p in sorted(SCHEMA_DIR.glob("*.schema.json"))]
    return Registry().with_resources((d["$id"], Resource.from_contents(d)) for d in docs)


def validate(document: dict[str, Any], kind: str) -> list[str]:
    """Schema errors (kind: variant | metadata.v2), as `path: message` lines.
    Empty means it validates."""
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(schema(kind), registry=_registry())
    out = []
    for error in sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path)):
        where = "/".join(str(p) for p in error.absolute_path) or "(root)"
        out.append(f"{where}: {error.message}")
    return out


def variant_problems(variant: dict[str, Any]) -> list[str]:
    """The rules model-catalog's validator adds on top of the schema for one
    variant file, plus the layer ownership swiss enforces when it loads one."""
    out = []
    values = variant.get("values") or {}
    requires = variant.get("requires") or {}
    lws = values.get("lws") or {}
    if lws.get("enabled") and lws.get("size") != requires.get("nodes"):
        out.append(
            f"values.lws.size {lws.get('size')} is not requires.nodes {requires.get('nodes')}"
        )
    chart = (variant.get("chart") or {}).get("name")
    if chart and chart != variant.get("engine"):
        out.append(f"chart.name {chart} is not its engine, {variant.get('engine')}")
    topology = requires.get("topology")
    if topology in ("lws", "pd") and (values.get(topology) or {}).get("enabled") is not True:
        out.append(f"requires.topology {topology} needs values.{topology}.enabled: true")
    foreign = sorted(_FOREIGN_VALUE_KEYS & set(values))
    if foreign:
        out.append(f"values writes site/deploy-owned keys: {', '.join(foreign)}")
    model = values.get("model") or {}
    for key in ("localPath", "gpus"):
        if key in model:
            out.append(f"values.model.{key} is not the catalog's to set")
    for arg in unmounted_paths(variant):
        out.append(
            f"{arg} reads a path no mount provides (weights at "
            f"{model.get('mountPath') or CHART_MODEL_MOUNT}); the engine would not find it"
        )
    return out


def unmounted_paths(variant: dict[str, Any]) -> list[str]:
    """extraArgs whose absolute path lies under neither the weights mount (the
    chart's default when the entry pins none) nor one of the entry's volumes --
    the flag names a file that will not exist in the container."""
    values = variant.get("values") or {}
    mounts = [((values.get("model") or {}).get("mountPath")) or CHART_MODEL_MOUNT]
    mounts += [str(m.get("mountPath") or "") for m in values.get("volumeMounts") or []]
    roots = [m.rstrip("/") for m in mounts if m]
    out = []
    for arg in values.get("extraArgs") or []:
        _, eq, value = str(arg).partition("=")
        if (
            eq
            and value.startswith("/")
            and not any(value == r or value.startswith(r + "/") for r in roots)
        ):
            out.append(str(arg))
    return out
