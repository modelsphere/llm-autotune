"""Which of a measured config's env vars and mounts belong in a catalog entry.

A config is measured on one site: its env and mounts mix the serving recipe
(engine knobs that change what is measured) with facts about that site (the
NIC an engine binds, a store's address, a cache directory on the host). A
catalog entry is served on other sites, so only the recipe belongs in it; a
site fact copied into it either breaks the deploy or quietly does nothing.

Every env var and mount gets a category, and the category decides the action:

  env       recipe    kept
            site      left out; the pull request lists it for deployers
            secret    left out
            platform  left out (the platform sets it per launch)
            unknown   kept, flagged: nothing says what it is
  mount     weights   left out: the chart mounts the weights itself
            model     a second model (a draft) a flag reads: kept, at the
                      catalog's convention /data/models/<name>, read-only
            config    a file a flag or env var reads: kept as measured
            scratch   a cache: an emptyDir, so the recipe behaves the same
            site      left out: nothing reads it

The rules come from what the catalog's own entries carry (which names are
tuning, which are the network a site runs on) and from what a value looks
like (an address, a host path no mount provides). A caller can settle a name
the rules do not know (`env_categories`), and that always wins.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

ENV_CATEGORIES = ("recipe", "site", "secret", "platform", "unknown")

# Set by the platform for each launch: which cards, never part of a recipe.
PLATFORM_ENV = frozenset({"CUDA_VISIBLE_DEVICES", "NVIDIA_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES"})
SECRET_ENV = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|ACCESS_KEY|CREDENTIAL", re.I)

# The network and storage a site runs on: which NIC and RDMA device, where a
# KV store lives, how to reach the internet, where libraries are installed.
SITE_ENV = frozenset(
    {
        "NCCL_SOCKET_IFNAME",
        "GLOO_SOCKET_IFNAME",
        "TP_SOCKET_IFNAME",
        "NCCL_SOCKET_FAMILY",
        "NCCL_IB_HCA",
        "NCCL_IB_GID_INDEX",
        "NCCL_IB_DISABLE",
        "NCCL_IB_TC",
        "NCCL_IB_SL",
        "NCCL_NET_GDR_LEVEL",
        "NCCL_NET",
        "NCCL_TOPO_FILE",
        "LD_LIBRARY_PATH",
        "PATH",
        "HF_ENDPOINT",
        "HF_HOME",
        "HF_HUB_CACHE",
        "HF_HUB_OFFLINE",
        "TRANSFORMERS_OFFLINE",
        "HF_DATASETS_OFFLINE",
        "MODELSCOPE_CACHE",
    }
)
SITE_ENV_PATTERN = re.compile(r"^(MOONCAKE_|MC_)|(^|_)PROXY$|^KUBERNETES_", re.I)

# Engine and runtime knobs: what the catalog's entries tune with.
RECIPE_ENV_PATTERN = re.compile(
    r"^(SGLANG_|VLLM_|TORCHINDUCTOR_|TORCH_|PYTORCH_|CUDA_|NCCL_|FLASHINFER_|TRITON_|"
    r"DG_|DEEPEP_|NVSHMEM_|SAFETENSORS_)"
    r"|^(OMP_NUM_THREADS|PYTHONHASHSEED|TOKENIZERS_PARALLELISM|NVIDIA_DISABLE_REQUIRE)$"
)

_ADDRESS = re.compile(r"(\b\d{1,3}(\.\d{1,3}){3}\b)|([a-z][a-z0-9+.-]*://)", re.I)

# Engine flags that name a second model's weights.
MODEL_FLAGS = frozenset({"speculative_draft_model_path", "draft_model", "speculative_model"})
# Where a catalog entry's sites keep model weights (what the hand-written
# entries give model.localPath).
MODELS_ROOT = "/data/models"
_SCRATCH = re.compile(r"(^|/)(\.?cache|\.triton|\.nv|tmp|compile[-_]cache[^/]*)(/|$)", re.I)


@dataclass
class Input:
    """One env var or mount of a measured config, and what the export does
    with it."""

    kind: str  # env | mount
    name: str  # the env var, or the mount's path in the container
    value: str  # the env value, or the host path
    category: str
    action: str  # kept | left out | emptyDir | moved
    reason: str

    def payload(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "name": self.name,
            "value": self.value,
            "category": self.category,
            "action": self.action,
            "reason": self.reason,
        }


@dataclass
class MountPlan:
    volumes: list[dict[str, Any]] = field(default_factory=list)
    mounts: list[dict[str, Any]] = field(default_factory=list)
    inputs: list[Input] = field(default_factory=list)
    # container paths a kept mount provides, for env values that point into one
    provided: list[str] = field(default_factory=list)


def _under(value: str, root: str) -> bool:
    root = root.rstrip("/")
    return bool(root) and (value == root or value.startswith(root + "/"))


def _paths(values: list[Any]) -> list[str]:
    """Absolute paths in flag and env values, including inside a JSON value
    (--speculative-config={"model": "/draft", ...})."""
    out = []
    for v in values:
        out += re.findall(r"(?<![\w.])(/[A-Za-z0-9._~/-]+)", str(v))
    return out


def _name(container: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", PurePosixPath(container).name.lower()).strip("-")
    return (base or "vol")[:50]


def plan_mounts(
    volumes: dict[str, str],
    engine_args: dict[str, Any],
    env: dict[str, str],
    weights_mount: str,
    keep_all: bool = False,
) -> MountPlan:
    """What each host mount becomes. `keep_all` publishes every mount as the
    host path it was (an explicit choice, warned about)."""
    plan = MountPlan()
    flag_paths = {k: _paths([v]) for k, v in (engine_args or {}).items()}
    env_paths = _paths(list((env or {}).values()))
    taken = {"dot-cache", "dshm"}
    for host, spec in (volumes or {}).items():
        container, _, mode = str(spec).partition(":")
        container = container.rstrip("/") or container
        name = _name(container)
        while name in taken:
            name = f"{name}-x"

        def add(kind: str, source: dict[str, Any], read_only: bool, at: str = container) -> None:
            taken.add(kind)
            plan.volumes.append({"name": kind, **source})
            mount: dict[str, Any] = {"name": kind, "mountPath": at}
            if read_only:
                mount["readOnly"] = True
            plan.mounts.append(mount)
            plan.provided.append(at)

        readers = [k for k, ps in flag_paths.items() if any(_under(p, container) for p in ps)]
        read_by_env = any(_under(p, container) for p in env_paths)
        if keep_all:
            add(name, {"hostPath": {"path": host, "type": "Directory"}}, mode == "ro")
            plan.inputs.append(
                Input("mount", container, host, "site", "kept", "host mounts included")
            )
        elif container in ("/dev/shm", "/root/.cache") or _under(container, weights_mount):
            plan.inputs.append(
                Input(
                    "mount",
                    container,
                    host,
                    "weights" if _under(container, weights_mount) else "scratch",
                    "left out",
                    "the chart provides it"
                    if not _under(container, weights_mount)
                    else "the model weights",
                )
            )
        elif any(k in MODEL_FLAGS for k in readers) or ("speculative_config" in readers):
            path = f"{MODELS_ROOT}/{PurePosixPath(host).name}"
            add("draft-model", {"hostPath": {"path": path, "type": "Directory"}}, True)
            plan.inputs.append(
                Input(
                    "mount",
                    container,
                    host,
                    "model",
                    "moved",
                    f"draft model, at {path}",
                )
            )
        elif readers or read_by_env:
            add(name, {"hostPath": {"path": host, "type": "Directory"}}, mode == "ro")
            who = ", ".join(f"--{r.replace('_', '-')}" for r in readers) or "an env var"
            plan.inputs.append(
                Input(
                    "mount",
                    container,
                    host,
                    "config",
                    "kept",
                    f"read by {who}",
                )
            )
        elif _SCRATCH.search(container):
            add(name, {"emptyDir": {"sizeLimit": "10Gi"}}, False)
            plan.inputs.append(
                Input(
                    "mount",
                    container,
                    host,
                    "scratch",
                    "emptyDir",
                    "cache",
                )
            )
        else:
            plan.inputs.append(Input("mount", container, host, "site", "left out", "unused"))
    return plan


def classify_env(
    name: str, value: str, provided: list[str], overrides: dict[str, str] | None = None
) -> tuple[str, str]:
    """(category, reason) for one env var. `provided`: container paths the
    entry's own mounts provide."""
    given = (overrides or {}).get(name)
    if given in ENV_CATEGORIES:
        return given, "set by you"
    if name in PLATFORM_ENV:
        return "platform", "set per run"
    if SECRET_ENV.search(name):
        return "secret", "a credential"
    if name in SITE_ENV or SITE_ENV_PATTERN.search(name):
        return "site", "specific to this cluster"
    if _ADDRESS.search(value):
        return "site", "an address"
    paths = _paths([value])
    if paths and not all(any(_under(p, root) for root in provided) for p in paths):
        return "site", "a path on this machine"
    if RECIPE_ENV_PATTERN.search(name):
        return "recipe", "engine setting"
    return "unknown", "not recognized"


ENV_ACTIONS = {
    "recipe": "kept",
    "unknown": "kept",
    "site": "left out",
    "secret": "left out",
    "platform": "left out",
}
