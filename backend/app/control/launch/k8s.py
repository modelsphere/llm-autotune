"""k8s driver — the GPU-cluster deployment substrate.

The bare-metal driver (ssh_docker) reaches a machine it controls: it picks the
GPUs, runs the container, stops production, puts it back. None of that is true
on the cluster. Here the scheduler owns placement — we *ask* for a workload of
N GPUs and *poll* it; we never choose a node or a device, never ssh anywhere,
and never stop the production services (the cluster's gitops owns those). So
this driver is deliberately thinner than the ssh one: render a LaunchSpec into
a workload, submit it, watch its status, delete it.

Shape (deliberately uncommitted — the cluster design is still TBD). The driver
commits only to the *lifecycle shape*: render a LaunchSpec into some object(s),
`apply` them, poll status, `attach()` by deterministic name, `teardown()` by
deleting them, and gate readiness on the workload being Ready. WHAT object it
renders is a setting, `k8s_workload_kind`:

- "deployment" (default): a plain `Deployment` + `NodePort` Service. The
  universal k8s primitive — works on any cluster, needs no CRD or operator, and
  is the right shape for single-node. This is the concrete path we can run
  today (validated against a local k3d cluster).
- "custom": a `TuningRun` the autotune-operator reconciles (see
  ../../autotune_operator). We hand it image + argv + port + GPU count + the
  model mount; the operator builds the pod, its NodePort Service, and the
  /v1/models readiness probe, then reports `status.phase` and `status.endpoint`,
  which state()/attach() read back. Group/version/kind come from settings.

When the design solidifies, only `render_workload`, the status mapping, and the
`AUTOTUNE_K8S_*` settings change; nothing above the driver moves. See
docs/findings_claude/k8s-and-cicd-seams.md.

What is NOT here on purpose:
- No node/device selection: `spec.gpu_indices` becomes a *count*; the cluster
  assigns actual devices. Packing several runs onto specific cards of one box
  is a bare-metal concern — on k8s the scheduler bin-packs.
- No production teardown: `capture/clear/restore_baseline` are no-ops that
  report "cluster-managed". The nightly baseline is therefore measured by
  *relaunching* the production config as its own workload, not by canarying an
  in-place service we are not allowed to stop.
"""

from __future__ import annotations

import json
import logging
import shlex
from typing import Any

from app.control.engines import MODEL_MOUNT, get_adapter
from app.control.launch.base import (
    DeploymentDriver,
    DeploymentHandle,
    DeploymentState,
    LaunchSpec,
    MachineInfo,
    WorkloadSpec,
    WorkloadState,
    merge_volumes,
)
from app.control.launch.clusters import K8sClusterSettings, as_cluster, resolve_cluster
from app.control.launch.failures import TRANSIENT_PLACEMENT_FAILURES
from app.control.launch.k8s_api import K8sApi, get_k8s_api
from app.core.config import Settings, get_settings
from app.hardware import normalize_gpu_type

logger = logging.getLogger(__name__)

# Same runtime env the ssh driver sets; declared here rather than imported so
# the two substrates can diverge (a cluster image may bake these in). Keep in
# sync deliberately, not accidentally.
DEFAULT_ENV = {
    "NVIDIA_DISABLE_REQUIRE": "1",
    "PYTORCH_ALLOC_CONF": "expandable_segments:True",
}

# Labels our objects (and, via the pod template, their pods) carry, so
# logs/state/exit-info find the pieces of one run without knowing any operator's
# naming scheme.
RUN_LABEL = "llm-autotune.io/run"
MANAGED_LABEL = "llm-autotune.io/managed"
# A policy container's Job and its pod. Distinct from RUN_LABEL: a policy is not
# a run, and its pods must never be selected by an engine's log or state read.
POLICY_LABEL = "llm-autotune.io/policy"

# TuningRun status.phase values (custom mode) that mean the workload is dead,
# not still coming up. "failed"/"expired" are the operator's; the rest are
# tolerated in case a different operator reports a raw k8s reason as the phase.
_FAILED_PHASES = {
    "failed", "expired", "error", "crashloopbackoff", "unschedulable",
    "imagepullbackoff", "errimagepull", "evicted",
}

# Pod container "waiting" reasons (deployment mode) that will never resolve on
# their own — a workload stuck in one of these is CRASHED, not STARTING.
_POD_FAILED_REASONS = {
    "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "InvalidImageName",
    "CreateContainerError", "CreateContainerConfigError", "RunContainerError",
}

# Pod *event* reasons (kubelet Warnings) that wedge a pod BEFORE any container
# starts, so they never show up as a container "waiting" reason and never reach
# the operator's phase. A pod stuck here (a hostPath model missing from the
# node, a volume that will not attach) never becomes ready — surface it. These
# are terminal for our workloads: we mount weights as a hostPath (no CSI attach
# to retry), so FailedMount here means the path is simply not on the node.
_POD_FAILED_EVENT_REASONS = {
    "FailedMount", "FailedAttachVolume", "FailedCreatePodSandBox",
}


def _failure_class_for(reason: str) -> str:
    """Map a k8s pod/event reason onto the platform's failure vocabulary."""
    return {
        "ImagePullBackOff": "image_pull",
        "ErrImagePull": "image_pull",
        "InvalidImageName": "image_pull",
        "CreateContainerConfigError": "config_error",
        "CreateContainerError": "config_error",
        "RunContainerError": "config_error",
        "CrashLoopBackOff": "crash_loop",
        "FailedMount": "mount_failed",
        "FailedAttachVolume": "mount_failed",
        "FailedCreatePodSandBox": "sandbox_failed",
    }.get(reason, reason.lower())


# Failure classes that are really "not placed YET", not "wedged" — defined once
# in the substrate-neutral vocabulary (app/control/launch/failures.py) and
# aliased here for the driver's own use. An unschedulable pod is queued: it
# schedules the moment a node frees the GPUs it asked for. That happens
# routinely between our own rounds — the previous run's pod is still
# Terminating, so the cluster has not reclaimed its cards yet — and whenever
# production holds the cluster. Treating it as a crash turns a wait into a
# failed run (and an immediate, wasteful relaunch), so it reads as "still
# coming up" for as long as the machine's lease lasts. Placement that can
# never succeed is a different class ("unplaceable") and fails at once.
_TRANSIENT_FAILURE_CLASSES = TRANSIENT_PLACEMENT_FAILURES


def _cr_resource(settings: Settings) -> str:
    """The `plural.group` string that addresses our CR through kubectl/the API
    (e.g. `tuningruns.tuning.llm-autotune.io`)."""
    return f"{settings.k8s_cr_plural}.{settings.k8s_cr_group}"


def _gpu_count(spec: LaunchSpec) -> int:
    """Cards to request from the scheduler. `gpu_indices` is a count here, not a
    placement: len() when the packer assigned some, else the machine's full
    complement (empty has always meant "all")."""
    if spec.gpu_indices:
        return len(spec.gpu_indices)
    return spec.machine.gpu_count or 0


def _node_selector(spec: LaunchSpec, settings: Settings) -> dict[str, str]:
    """The nodeSelector constraining where this run's pod may land, as a dict.

    Sourced from the MACHINE ("k=v,k2=v2") — a k8s machine is a named node-slice,
    so its selector is a property of the machine, set on the Resources page — and
    falls back to the global AUTOTUNE_K8S_NODE_SELECTOR only when the machine
    declares none."""
    return _node_selector_pairs(spec.machine.node_selector, settings)


def _node_selector_pairs(raw: str, settings: Settings) -> dict[str, str]:
    """Parse "k=v,k2=v2" into a dict, falling back to the global selector when
    empty. Tolerant of stray spaces and empty/degenerate entries."""
    raw = raw or settings.k8s_node_selector
    out: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if "=" not in pair:
            continue
        key, value = (part.strip() for part in pair.split("=", 1))
        if key:
            out[key] = value
    return out


def _labels(spec: LaunchSpec) -> dict[str, str]:
    return {MANAGED_LABEL: "true", RUN_LABEL: str(spec.run_id)}


def render_workload(spec: LaunchSpec, settings: Settings | None = None) -> list[dict]:
    """A LaunchSpec as the k8s object(s) to apply for one run.

    Returns a LIST because a workload is not always one object (a Deployment
    travels with its Service). Declarative and pure — the k8s analogue of
    `render_docker_command`. The engine argv comes from the same adapter the
    ssh driver uses (`app.control.engines`), so a config serves identically on
    both substrates; only the envelope differs.

    Dispatches on `k8s_workload_kind`; each branch is the single place its
    object schema is assumed, and the only thing that changes when the cluster
    design solidifies.
    """
    settings = settings or get_settings()
    if settings.k8s_workload_kind == "deployment":
        return [_render_deployment(spec, settings), _render_service(spec, settings)]
    return [_render_cr(spec, settings)]


def _guarded_command(command: list[str]) -> list[str]:
    """The engine launch, preceded by a one-line check that the model weights are
    actually at the mount before the engine starts.

    On k8s the scheduler places the pod, and the weights are a per-node hostPath
    today — so a pod can land on a node that is missing the model. Without this
    the engine (sglang/vllm) starts, spends a minute or more discovering the
    weights are absent, and only then crashes. The guard fails in ~1s instead,
    with a log line that says why, so the run is classified and retried (or
    surfaced) immediately rather than burning the ready window. `exec` hands the
    engine PID 1 so signals/teardown behave exactly as before. Covers both the
    "dir missing" and "mounted but empty" cases, in both deployment and custom
    mode (it rides in the container command, not the pod shape)."""
    engine = " ".join(shlex.quote(part) for part in command)
    mount = shlex.quote(MODEL_MOUNT)
    guard = (
        f'if [ -z "$(ls -A {mount} 2>/dev/null)" ]; then '
        f'echo "FATAL: no model weights at {MODEL_MOUNT} — this node is missing the '
        f'model hostPath; failing fast instead of starting the engine" >&2; exit 66; fi; '
        f'exec {engine}'
    )
    return ["/bin/sh", "-c", guard]


def _engine_container(spec: LaunchSpec, settings: Settings) -> dict:
    """The serving container, shared by the Deployment (and reusable by a CRD
    that takes a raw pod template)."""
    command = _guarded_command(get_adapter(spec.engine).build_command(spec))
    env = {**DEFAULT_ENV, **spec.env}
    pod_volumes, mounts = _engine_volumes(spec, settings)
    container: dict[str, Any] = {
        "name": "engine",
        "image": spec.image,
        # Prefer an image already on the node (the production manifests all do):
        # engine images are large and often pre-pulled, and a pull inside the
        # run's clock is how a large image turns into a readiness timeout.
        "imagePullPolicy": "IfNotPresent",
        # element 0 is the entrypoint; the rest are args — same split the ssh
        # driver's `--entrypoint` makes.
        "command": [command[0]],
        "args": command[1:],
        "ports": [{"containerPort": spec.port}],
        "env": [{"name": k, "value": str(v)} for k, v in sorted(env.items())],
        # Readiness is the k8s-native signal: kubelet probes /v1/models from
        # inside the cluster (where the pod is always reachable), so the driver
        # does not need to reach the endpoint itself to know it is up. No
        # livenessProbe — a big model loads for minutes and would be killed.
        "readinessProbe": {
            "httpGet": {"path": "/v1/models", "port": spec.port},
            "initialDelaySeconds": 10,
            "periodSeconds": 10,
            "failureThreshold": 120,  # ~20 min of loading tolerated
        },
        "volumeMounts": mounts,
    }
    # GPU-ness is per machine, exactly as in the ssh driver: only a machine with
    # cards gets the resource request and the nvidia runtimeClass. This is also
    # what lets the driver run on a GPU-less test cluster.
    gpus = _gpu_count(spec)
    pod_spec: dict[str, Any] = {"containers": [container], "volumes": pod_volumes}
    pull_secrets = _image_pull_secrets(settings)
    if pull_secrets:
        pod_spec["imagePullSecrets"] = pull_secrets
    if gpus > 0:
        limits: dict[str, Any] = {settings.k8s_gpu_resource: gpus}
        requests: dict[str, Any] = {settings.k8s_gpu_resource: gpus}
        if settings.k8s_engine_cpu_limit:
            limits["cpu"] = settings.k8s_engine_cpu_limit
        if settings.k8s_engine_memory_limit:
            limits["memory"] = settings.k8s_engine_memory_limit
        if settings.k8s_engine_cpu_request:
            requests["cpu"] = settings.k8s_engine_cpu_request
        if settings.k8s_engine_memory_request:
            requests["memory"] = settings.k8s_engine_memory_request
        # A namespace with a LimitRange/quota that demands cpu/memory requests
        # rejects a pod that only limits the GPU, so emit requests when the
        # cluster asks for them. Left empty, the manifest is exactly what it
        # was before these knobs existed.
        container["resources"] = (
            {"limits": limits, "requests": requests} if len(requests) > 1 else {"limits": limits}
        )
        if settings.k8s_runtime_class:
            pod_spec["runtimeClassName"] = settings.k8s_runtime_class
        # The safe substitute for the ssh driver's `--ipc=host`: a sized in-memory
        # /dev/shm, so NCCL/multi-GPU has the shared memory it needs without
        # sharing the node's IPC namespace (which hostIPC:true would).
        if settings.k8s_shm_size_mb > 0:
            container["volumeMounts"].append({"name": "dshm", "mountPath": "/dev/shm"})
            pod_spec["volumes"].append({
                "name": "dshm",
                "emptyDir": {"medium": "Memory", "sizeLimit": f"{settings.k8s_shm_size_mb}Mi"},
            })
    selector = _node_selector(spec, settings)
    if selector:
        pod_spec["nodeSelector"] = selector
    tolerations = _tolerations(gpus, settings)
    if tolerations:
        pod_spec["tolerations"] = tolerations
    return pod_spec


def _tolerations(gpus: int, settings: Settings) -> list[dict[str, Any]]:
    """Which node taints this pod may ignore.

    A GPU taint keeps pods that do not want cards OFF a GPU node; it is not
    meant to keep GPU work away. Every GPU workload on our cluster — production
    serving, the device plugins, the RDMA daemons — carries the matching
    toleration, so a pod that asks for cards gets it too. Anything else is
    declared explicitly in `k8s_tolerations`.
    """
    out: list[dict[str, Any]] = []
    if gpus > 0 and settings.k8s_tolerate_gpu_taint:
        out.append(
            {"key": settings.k8s_gpu_resource, "operator": "Exists", "effect": "NoSchedule"}
        )
    for raw in (settings.k8s_tolerations or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        key, _, effect = raw.partition(":")
        key, sep, value = key.partition("=")
        entry: dict[str, Any] = {"key": key.strip()}
        if sep:
            entry["operator"] = "Equal"
            entry["value"] = value.strip()
        else:
            entry["operator"] = "Exists"
        if effect.strip():
            entry["effect"] = effect.strip()
        out.append(entry)
    return out


def _image_pull_secrets(settings: Settings) -> list[dict[str, str]]:
    """Registry credentials for every pod we create. Usually empty — nodes
    often already hold credentials for the registry the engine image lives in
    — and when it is, the field must not be rendered at all: an empty list is
    not the same as no field to a strict admission webhook."""
    return [
        {"name": name.strip()}
        for name in (settings.k8s_image_pull_secrets or "").split(",")
        if name.strip()
    ]


def _model_subpath(host_path: str, settings: Settings) -> str:
    """The campaign's absolute model path, expressed relative to the PVC root.

    A shared weights claim is mounted once and each run exposes only its own
    directory, so `/mnt/disk0/models/modelforge/r1` under a claim rooted at
    `/mnt/disk0/models` becomes subPath `modelforge/r1`. A path that does not
    live under the declared root is passed through with its leading slash
    stripped rather than silently rewritten: it will be visibly wrong in the
    manifest instead of quietly mounting the wrong directory.
    """
    root = (settings.k8s_model_pvc_root or "").rstrip("/")
    path = host_path
    if root and (path == root or path.startswith(root + "/")):
        path = path[len(root) :]
    return path.strip("/")


def _engine_volumes(
    spec: LaunchSpec, settings: Settings
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(pod volumes, container mounts) for the engine, paired by construction.

    Every mount is a hostPath except the model, which becomes a subPath of a
    shared PVC when one is configured — the difference between "this run can
    only go where someone staged the weights" and "the scheduler may place it
    anywhere". Volumes and mounts are derived from the SAME ordered pairs:
    naming them from two independent sorts (of host paths and of container
    paths) happens to agree for the single model mount and can mispair the
    moment a campaign declares a second volume.
    """
    pvc = (settings.k8s_model_pvc or "").strip()
    volumes: list[dict[str, Any]] = []
    mounts: list[dict[str, Any]] = []
    pairs = sorted(merge_volumes({spec.model_path: MODEL_MOUNT}, spec.volumes).items())
    for i, (host_path, container_path) in enumerate(pairs):
        name = _vol_name(i)
        mount: dict[str, Any] = {"name": name, "mountPath": container_path}
        if pvc and container_path == MODEL_MOUNT:
            volumes.append({"name": name, "persistentVolumeClaim": {"claimName": pvc}})
            sub = _model_subpath(host_path, settings)
            if sub:
                mount["subPath"] = sub
        else:
            volumes.append({"name": name, "hostPath": {"path": host_path}})
        mounts.append(mount)
    return volumes, mounts


def _vol_name(i: int) -> str:
    return "vol-0" if i == 0 else f"vol-{i}"


def _render_deployment(spec: LaunchSpec, settings: Settings) -> dict:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": spec.container_name,  # deterministic: autotune-run-<id>
            "namespace": settings.k8s_namespace,
            "labels": _labels(spec),
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {RUN_LABEL: str(spec.run_id)}},
            "template": {
                "metadata": {"labels": _labels(spec)},
                "spec": _engine_container(spec, settings),
            },
        },
    }


def _render_service(spec: LaunchSpec, settings: Settings) -> dict:
    port: dict[str, Any] = {"name": "http", "port": spec.port, "targetPort": spec.port}
    if settings.k8s_service_nodeport:
        port["nodePort"] = settings.k8s_service_nodeport
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": spec.container_name,
            "namespace": settings.k8s_namespace,
            "labels": _labels(spec),
        },
        "spec": {
            "type": "NodePort",
            "selector": {RUN_LABEL: str(spec.run_id)},
            "ports": [port],
        },
    }


def render_policy_job(spec: WorkloadSpec, settings: Settings) -> dict:
    """One policy container as a `batch/v1` Job.

    A Job, not a Deployment or a TuningRun, because of what a policy IS. A
    TuningRun describes something that SERVES — port, readiness probe, NodePort
    Service, weights — and a policy has none of those; forcing it in would mean
    a fake port and a readiness probe that can never pass. A Deployment would
    resurrect a policy that finished its night cleanly. A Job is "run this to
    completion and stop", which is exactly `docker run -d` with no `--restart`,
    the semantics the ssh driver already has.

    Placement is conditional, and that is the point of running the controller
    here at all: a delegated-only policy (gpu_indices None, no volumes) asks for
    no cards, inherits no nodeSelector and mounts nothing, so the scheduler may
    put it on any node — including a CPU one — instead of parking it on
    contended GPU hardware. A self-serving policy runs engines inside its own
    container, so it takes the cards, the machine's node selector and the model
    mount, like an engine would.
    """
    name = spec.name
    gpus = len(spec.gpu_indices) if spec.gpu_indices else 0
    wants_hardware = spec.gpu_indices is not None

    container: dict[str, Any] = {
        "name": "policy",
        "image": spec.image,
        "env": [{"name": k, "value": str(v)} for k, v in sorted(spec.env.items())],
        # Not BestEffort: a policy with no requests is first to be evicted under
        # node pressure, and losing the controller at 3am ends the night.
        "resources": {
            "requests": {
                "cpu": settings.k8s_policy_cpu_request,
                "memory": settings.k8s_policy_memory_request,
            }
        },
    }
    if spec.command:
        container["command"] = list(spec.command)
    if gpus:
        container["resources"]["limits"] = {settings.k8s_gpu_resource: gpus}

    volumes, mounts = _workload_volumes(spec)
    if mounts:
        container["volumeMounts"] = mounts

    pod_spec: dict[str, Any] = {
        "restartPolicy": "Never",
        # A policy image is user-supplied — that is the whole point of
        # policy-as-code — and must never be handed a credential to the cluster
        # API. Its only credential is the scoped AUTOTUNE_API_KEY in its env.
        "automountServiceAccountToken": False,
        "containers": [container],
    }
    pull_secrets = _image_pull_secrets(settings)
    if pull_secrets:
        pod_spec["imagePullSecrets"] = pull_secrets
    if volumes:
        pod_spec["volumes"] = volumes
    if wants_hardware:
        selector = _node_selector_pairs(spec.machine.node_selector, settings)
        if selector:
            pod_spec["nodeSelector"] = selector
        if gpus and settings.k8s_runtime_class:
            pod_spec["runtimeClassName"] = settings.k8s_runtime_class
    if settings.k8s_policy_priority_class:
        pod_spec["priorityClassName"] = settings.k8s_policy_priority_class

    job: dict[str, Any] = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "labels": {MANAGED_LABEL: "true", POLICY_LABEL: name},
        },
        "spec": {
            # An exit is final, exactly as it is on docker. The SDK cannot
            # currently resume a restarted container — its idempotency tags
            # restart at t1 and collide with the previous incarnation's, and its
            # in-flight launches are in-memory and would be orphaned holding the
            # session's own cards — so a retry would be worse than a clean death.
            "backoffLimit": 0,
            # Long enough to be a leak guard, never short enough to race the
            # post-mortem: the platform reads a dead policy's logs through
            # driver.logs(), and a reaped pod takes them with it.
            "ttlSecondsAfterFinished": settings.k8s_policy_ttl_after_finished,
            "template": {
                "metadata": {"labels": {MANAGED_LABEL: "true", POLICY_LABEL: name}},
                "spec": pod_spec,
            },
        },
    }
    if settings.k8s_policy_deadline_seconds:
        # A backstop, deliberately far beyond any real session. The platform's
        # own clocks (lease drain, window cutoff, idle strikes) end a session
        # gracefully and let it finalize; this one just kills the pod, so it
        # must never be the thing that fires first.
        job["spec"]["activeDeadlineSeconds"] = settings.k8s_policy_deadline_seconds
    return job


def _workload_volumes(spec: WorkloadSpec) -> tuple[list[dict], list[dict]]:
    """host path -> container path, as k8s hostPath volumes + mounts.

    The platform speaks docker's volume language; a cluster needs the pair split
    apart. ":ro" on the container path is respected, as it is on docker.
    """
    volumes: list[dict] = []
    mounts: list[dict] = []
    for index, (host_path, target) in enumerate(sorted((spec.volumes or {}).items())):
        mount_path, _, suffix = str(target).partition(":")
        volumes.append({"name": _vol_name(index), "hostPath": {"path": host_path}})
        mounts.append({
            "name": _vol_name(index),
            "mountPath": mount_path,
            "readOnly": suffix.strip() == "ro",
        })
    return volumes, mounts


def _render_cr(spec: LaunchSpec, settings: Settings) -> dict:
    """A `TuningRun` (tuning.llm-autotune.io) for the autotune-operator to reconcile.

    The operator is engine-agnostic: the argv is built by the SAME adapter the
    ssh/deployment paths use and passed through as command/args (element 0 the
    entrypoint, the rest args). The operator reads only the structured fields
    (port, gpuCount, the model mount) to build the pod, its NodePort Service,
    and the /v1/models readiness probe.

    One limitation vs deployment mode: the TuningRun models a single model mount
    (matching how the cluster serves today), so extra `spec.volumes` are not
    expressed. The common case — one model directory — is covered.
    """
    command = _guarded_command(get_adapter(spec.engine).build_command(spec))
    env = {**DEFAULT_ENV, **spec.env}
    cr_spec: dict[str, Any] = {
        "image": spec.image,
        "command": [command[0]],
        "args": command[1:],
        "port": spec.port,
        # A count, never device indices — the scheduler places them.
        "gpuCount": _gpu_count(spec),
        "gpuResourceName": settings.k8s_gpu_resource,
        "modelMountPath": MODEL_MOUNT,
        "serviceType": "NodePort",
        "env": {k: str(v) for k, v in sorted(env.items())},
    }
    # Weights: a shared claim if the cluster offers one (any node can then take
    # the run), else the per-node host directory the campaign names.
    pvc = (settings.k8s_model_pvc or "").strip()
    if pvc:
        cr_spec["modelPVC"] = pvc
        sub = _model_subpath(spec.model_path, settings)
        if sub:
            cr_spec["modelSubPath"] = sub
    else:
        # host model dir -> the container path the argv already references.
        cr_spec["modelHostPath"] = spec.model_path
    # Which node taints this run may ignore. The CRD carried no tolerations
    # field before operator 0.2.0: against an older operator the API server
    # prunes this silently and a pod on a tainted GPU pool stays Pending,
    # naming nothing. INSTALL.md states the version this backend expects.
    tolerations = _tolerations(_gpu_count(spec), settings)
    if tolerations:
        cr_spec["tolerations"] = tolerations
    pull_secrets = [s["name"] for s in _image_pull_secrets(settings)]
    if pull_secrets:
        cr_spec["imagePullSecrets"] = pull_secrets
    # Safe substitute for `--ipc=host`: the operator turns sharedMemoryMB into an
    # in-memory /dev/shm emptyDir (see ../../autotune_operator). GPU pods only.
    if _gpu_count(spec) > 0 and settings.k8s_shm_size_mb > 0:
        cr_spec["sharedMemoryMB"] = settings.k8s_shm_size_mb
    if settings.k8s_run_ttl_seconds:
        cr_spec["ttlSeconds"] = settings.k8s_run_ttl_seconds
    selector = _node_selector(spec, settings)
    if selector:
        cr_spec["nodeSelector"] = selector
    return {
        "apiVersion": f"{settings.k8s_cr_group}/{settings.k8s_cr_version}",
        "kind": settings.k8s_cr_kind,
        "metadata": {
            "name": spec.container_name,
            "namespace": settings.k8s_namespace,
            "labels": _labels(spec),
        },
        "spec": cr_spec,
    }


def _phase(cr: dict) -> str:
    """The CR's phase (custom mode), lower-cased, from whichever status field the
    operator populates. Tolerant by design: the schema is TBD."""
    status = (cr or {}).get("status") or {}
    for key in ("phase", "state"):
        value = status.get(key)
        if isinstance(value, str) and value:
            return value.lower()
    conditions = status.get("conditions") or []
    if conditions and isinstance(conditions[-1], dict):
        return str(conditions[-1].get("type", "")).lower()
    return ""


def _endpoint_from_cr(cr: dict | None, spec: LaunchSpec) -> str:
    """Custom mode: prefer a URL the operator publishes in status; fall back to
    the spec's host:port."""
    status = (cr or {}).get("status") or {}
    url = status.get("url") or status.get("endpoint")
    return url if isinstance(url, str) and url else spec.endpoint_url


# The node labels GPU-feature-discovery publishes, so capacity and card type are
# a read of the cluster rather than a hand-typed guess. `product` is the exact
# model ("NVIDIA-H100-80GB-HBM3"); allocatable `nvidia.com/gpu` is the count the
# scheduler will actually hand out.
_GPU_PRODUCT_LABEL = "nvidia.com/gpu.product"
_GPU_RESOURCE = "nvidia.com/gpu"


def _pod_gpu_request(pod: dict, resource: str) -> int:
    """GPUs this pod asks the scheduler for, summed over its containers and read
    from the pod's own spec — the number the scheduler actually weighed, not the
    one we believe we requested."""
    total = 0
    for container in ((pod.get("spec") or {}).get("containers") or []):
        limits = (container.get("resources") or {}).get("limits") or {}
        try:
            total += int(limits.get(resource, 0) or 0)
        except (TypeError, ValueError):
            continue
    return total


def _node_name(node: dict) -> str:
    return str((node.get("metadata") or {}).get("name") or "")


def _node_gpu_count(node: dict) -> int:
    """Allocatable GPUs on a node — what the scheduler can place, not the raw
    installed count (which a drained or partly-reserved node overstates)."""
    alloc = (node.get("status") or {}).get("allocatable") or {}
    try:
        return int(alloc.get(_GPU_RESOURCE, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _node_gpu_type(node: dict) -> str:
    """The node's canonical card type from its GPU product label, or "" if the
    label is absent or names a card the platform does not know yet."""
    labels = (node.get("metadata") or {}).get("labels") or {}
    return normalize_gpu_type(str(labels.get(_GPU_PRODUCT_LABEL) or ""))


class K8sDriver(DeploymentDriver):
    name = "k8s"

    def __init__(
        self,
        api: K8sApi | None = None,
        settings: Settings | K8sClusterSettings | None = None,
        cluster_id: int | None = None,
    ):
        # `settings` is the pre-clusters / test injection path: a Settings (or an
        # already-resolved cluster) pins this driver to that one object. Real
        # callers pass `cluster_id` and the machine's cluster is resolved from
        # the `clusters` table, falling back to `AUTOTUNE_K8S_*` when it is None.
        self.settings = (
            as_cluster(settings) if settings is not None else resolve_cluster(cluster_id)
        )
        # Built lazily via the factory unless injected (tests pass a fake). The
        # default transport is `unavailable`, so a misconfigured platform fails
        # with a specific message rather than a cryptic connection error.
        self._api = api

    @property
    def api(self) -> K8sApi:
        if self._api is None:
            self._api = get_k8s_api(self.settings)
        return self._api

    @property
    def _mode(self) -> str:
        return self.settings.k8s_workload_kind

    @property
    def _resource(self) -> str:
        """The resource the run's primary object lives under — a Deployment, or
        the CR. get/attach/teardown address this."""
        return "deployment" if self._mode == "deployment" else _cr_resource(self.settings)

    def _run_selector(self, spec_or_handle) -> str:
        name = getattr(spec_or_handle, "container_name", "") or ""
        if getattr(spec_or_handle, "workload", False):
            # A policy container is a Job, and its pod carries our own policy
            # label. Falling through to the engine branches would be actively
            # wrong, not merely empty: in deployment mode the run id is parsed
            # off the end of the name, so `autotune-policy-5` would select
            # ENGINE run 5's pods and read another run's logs.
            return f"{POLICY_LABEL}={name}"
        if self._mode != "deployment":
            # Custom mode: the operator owns the pods and labels them its own way
            # (value = the run's object name), so logs/environment/exit-info must
            # select on THAT label — not the one the driver sets itself, which is
            # only on the objects the driver creates in deployment mode.
            return f"{self.settings.k8s_cr_pod_label}={name}"
        # Deployment mode: the driver labels its own pods by run id. A handle
        # carries the container name (autotune-run-<id>); recover the id from it
        # so logs/state work from either a spec or a handle.
        run = getattr(spec_or_handle, "run_id", None)
        if run is None:
            run = name.rsplit("-", 1)[-1] if name else ""
        return f"{RUN_LABEL}={run}"

    # -- DeploymentDriver ----------------------------------------------------

    def launch(self, spec: LaunchSpec) -> tuple[DeploymentHandle, str]:
        manifests = render_workload(spec, self.settings)
        # apply, not create: idempotent, so a relaunch of the same run replaces
        # any stale objects rather than colliding on the name.
        self.api.apply(manifests)
        handle = DeploymentHandle(
            driver=self.name,
            container_name=spec.container_name,
            machine=spec.machine,
            endpoint_url=self._resolve_endpoint(spec),
        )
        # The "exact launch command" is the manifest set it was applied from —
        # the reproducible artifact, the way the docker run line is for ssh.
        return handle, f"kubectl apply -f - <<'EOF'\n{_yaml_ish(manifests)}\nEOF"

    def state(self, handle: DeploymentHandle) -> DeploymentState:
        primary = self.api.get(self._resource, handle.container_name)
        if primary is None:
            return DeploymentState.GONE
        if self._mode == "deployment":
            # Readiness comes from the pods' Ready condition — kubelet's own
            # /v1/models probe — not an HTTP call from the worker. That is both
            # more correct (kubelet reaches the pod IP; the worker may not) and
            # what lets this work without cluster-external networking.
            return self._pod_state(self._run_selector(handle))
        # custom mode: the operator owns readiness — it gates on the pod's own
        # /v1/models probe (kubelet, from inside the cluster) and reports the
        # result as status.phase. Trust that rather than probing the endpoint
        # ourselves, which the worker often cannot reach. Ready → READY; a
        # failed/expired phase → CRASHED; anything else (pending/starting) →
        # still coming up.
        phase = _phase(primary)
        if phase in _FAILED_PHASES:
            return DeploymentState.CRASHED
        if phase == "ready":
            return DeploymentState.READY
        # The operator reports "starting" until its own /v1/models probe passes,
        # which never happens for a pod wedged before the engine runs (a model
        # hostPath missing from the node, an unpullable image). The operator does
        # not surface those, so inspect the pod ourselves and fail fast with a
        # real reason — EXCEPT the transient ones (unschedulable), which are a
        # queue, not a wedge: those read as still-coming-up so the pod gets its
        # chance to schedule when a node frees GPUs, bounded by the startup
        # timeout rather than failed on the spot.
        reason = self.failure_reason(handle)
        if reason is not None and reason[0] not in _TRANSIENT_FAILURE_CLASSES:
            return DeploymentState.CRASHED
        return DeploymentState.STARTING

    def _pod_state(self, selector: str) -> DeploymentState:
        """READY/CRASHED/STARTING from the run's pods. Called only when the
        primary object exists, so 'no pods yet' is STARTING (scheduling), never
        GONE."""
        try:
            pods = self.api.list("pods", selector)
        except Exception:
            return DeploymentState.STARTING
        any_crashed = False
        for pod in pods:
            status = pod.get("status") or {}
            for cond in status.get("conditions") or []:
                if cond.get("type") == "Ready" and cond.get("status") == "True":
                    return DeploymentState.READY
            for container in status.get("containerStatuses") or []:
                waiting = (container.get("state") or {}).get("waiting") or {}
                if waiting.get("reason") in _POD_FAILED_REASONS:
                    any_crashed = True
                terminated = (container.get("state") or {}).get("terminated") or {}
                if terminated and terminated.get("exitCode") not in (0, None):
                    any_crashed = True
        return DeploymentState.CRASHED if any_crashed else DeploymentState.STARTING

    def logs(self, handle: DeploymentHandle, tail: int = 200) -> str:
        return self.api.logs(self._run_selector(handle), tail=tail)

    def teardown(self, handle: DeploymentHandle) -> None:
        # Idempotent by contract; the transport treats NotFound as success.
        if handle.workload:
            # The transport deletes a Job with Background propagation, so the
            # pod goes with it rather than being orphaned still running.
            self.api.delete("job", handle.container_name)
            return
        self.api.delete(self._resource, handle.container_name)
        if self._mode == "deployment":
            # The Service shares the run's name; delete it too so a pool is not
            # slowly filled with orphan Services.
            self.api.delete("service", handle.container_name)

    def attach(self, spec: LaunchSpec) -> DeploymentHandle | None:
        if self.api.get(self._resource, spec.container_name) is None:
            return None
        return DeploymentHandle(
            driver=self.name,
            container_name=spec.container_name,
            machine=spec.machine,
            endpoint_url=self._resolve_endpoint(spec),
        )

    def exit_info(self, handle: DeploymentHandle) -> tuple[int | None, bool]:
        """Exit code + OOM flag from the run's pods, when the cluster still
        holds them. Best-effort."""
        try:
            pods = self.api.list("pods", self._run_selector(handle))
        except Exception:
            return (None, False)
        for pod in pods:
            statuses = (pod.get("status") or {}).get("containerStatuses") or []
            for container in statuses:
                terminated = (container.get("state") or {}).get("terminated")
                if terminated:
                    code = terminated.get("exitCode")
                    oom = terminated.get("reason") == "OOMKilled"
                    return (code if isinstance(code, int) else None, oom)
        return (None, False)

    def failure_reason(self, handle: DeploymentHandle) -> tuple[str, str] | None:
        """Why the run's pod is wedged, as (failure_class, human message), read
        straight from the pod and its events — so a run that never produced a
        log still carries a real reason (FailedMount, ImagePullBackOff,
        Unschedulable, …) instead of a generic "not ready" timeout. None when
        nothing looks wrong. Best-effort: a transport that cannot read pods or
        events simply yields no reason."""
        try:
            pods = self.api.list("pods", self._run_selector(handle))
        except Exception:
            return None
        for pod in pods:
            status = pod.get("status") or {}
            for container in status.get("containerStatuses") or []:
                waiting = (container.get("state") or {}).get("waiting") or {}
                reason = waiting.get("reason")
                if reason in _POD_FAILED_REASONS:
                    return (_failure_class_for(reason), waiting.get("message") or reason)
                term = (container.get("state") or {}).get("terminated") or {}
                if term and term.get("exitCode") not in (0, None):
                    if term.get("reason") == "OOMKilled":
                        return ("oom", "engine container was OOMKilled")
                    return (
                        "engine_exited",
                        f"container exited with code {term.get('exitCode')} "
                        f"({term.get('reason') or 'Error'})",
                    )
            for cond in status.get("conditions") or []:
                if (
                    cond.get("type") == "PodScheduled"
                    and cond.get("status") == "False"
                    and cond.get("reason") == "Unschedulable"
                ):
                    message = cond.get("message") or "no node fits the pod"
                    return self._placement_verdict(handle, pod, message)
            # Scheduled but stuck before the container starts — the reason lives
            # only in the pod's Warning events (e.g. a hostPath model missing).
            event_failure = self._event_failure(pod)
            if event_failure is not None:
                return event_failure
        return None

    def _placement_verdict(
        self, handle: DeploymentHandle, pod: dict, message: str
    ) -> tuple[str, str]:
        """Unschedulable means one of two opposite things, and the difference
        decides whether the run waits or dies.

        BUSY — every node that could take this pod is full. Waiting is the
        whole answer: the pod schedules the moment production or a previous
        round gives cards back. This is the normal state of a shared cluster,
        and a lease on a k8s pool is a licence to sit in that queue.

        IMPOSSIBLE — no node could ever take it: a `node_selector` matching
        nothing (a typo, or a node that left the cluster), or more cards than
        the biggest matched node physically has. Waiting here is a campaign
        quietly doing nothing until its lease expires, so it fails now, with
        the arithmetic that proves it.

        The GPU request is read from the pod's own spec rather than from what
        we think we asked for — the operator renders the pod, so its spec is
        what the scheduler actually weighed. Anything we cannot read leaves
        the verdict at "busy": failing a run needs proof, waiting does not.
        """
        selector = (handle.machine.node_selector or self.settings.k8s_node_selector).strip()
        try:
            nodes = (
                self.api.list("nodes", label_selector=selector)
                if selector
                else self.api.list("nodes")
            )
        except Exception:
            return ("unschedulable", message)
        if selector and not nodes:
            return (
                "unplaceable",
                f"no cluster node matches this machine's node selector ({selector}); "
                "the pod can never be scheduled — fix the selector on the Resources page",
            )
        want = _pod_gpu_request(pod, self.settings.k8s_gpu_resource)
        if want > 0 and nodes:
            biggest = max((_node_gpu_count(node) for node in nodes), default=0)
            if want > biggest:
                return (
                    "unplaceable",
                    f"pod asks for {want} x {self.settings.k8s_gpu_resource} but the "
                    f"largest node matching {selector or 'this cluster'} has {biggest} "
                    "allocatable; no amount of waiting fits it",
                )
        return ("unschedulable", message)

    def _event_failure(self, pod: dict) -> tuple[str, str] | None:
        """The newest terminal Warning event for a pod, as (failure_class,
        message). Best-effort — needs the transport to list events."""
        name = (pod.get("metadata") or {}).get("name")
        if not name:
            return None
        try:
            events = self.api.list("events")
        except Exception:
            return None
        best: tuple[str, str, str] | None = None  # (timestamp, reason, message)
        for event in events:
            if event.get("type") != "Warning":
                continue
            involved = event.get("involvedObject") or {}
            if involved.get("kind") != "Pod" or involved.get("name") != name:
                continue
            reason = event.get("reason")
            if reason not in _POD_FAILED_EVENT_REASONS:
                continue
            ts = event.get("lastTimestamp") or event.get("eventTime") or ""
            if best is None or ts >= best[0]:
                best = (ts, reason, event.get("message") or "")
        if best is None:
            return None
        _, reason, message = best
        return (_failure_class_for(reason), message or reason)

    def startup_status(self, handle: DeploymentHandle) -> dict[str, Any]:
        """Whether the run's pod is still coming up (scheduling / pulling its
        image) or has a container actually running.

        Read straight from the pod: any container with a `running` state means
        the image is pulled and the engine process is up (loading the model);
        anything else — no pods yet, or containers still `waiting` — is the
        pull/schedule phase. Best-effort; an unreadable pod reads as 'unknown'
        so the caller keeps its single-timeout behaviour."""
        try:
            pods = self.api.list("pods", self._run_selector(handle))
        except Exception:
            return {"phase": "unknown", "running_since": None}
        if not pods:
            # The primary object exists (checked before this is called) but no
            # pod is scheduled yet — still coming up, not wedged.
            return {"phase": "pulling", "running_since": None}
        running_since: str | None = None
        for pod in pods:
            for container in (pod.get("status") or {}).get("containerStatuses") or []:
                started = ((container.get("state") or {}).get("running") or {}).get("startedAt")
                if started and (running_since is None or started < running_since):
                    running_since = started  # earliest running container
        if running_since:
            return {"phase": "running", "running_since": running_since}
        return {"phase": "pulling", "running_since": None}

    def placement(self, handle: DeploymentHandle) -> str:
        """"placed" once the scheduler has bound a pod to a node, "queued"
        while it has not.

        `spec.nodeName` is the binding itself: set, and the cluster has
        committed this run's cards to it — the run owns hardware even while the
        image pulls. Unset (or no pod at all yet) means the request is still
        only a request, holding nothing and costing nothing to withdraw.

        A transport that cannot read pods answers "unknown", never "queued":
        callers withdraw queued runs, and withdrawing on a failed read would
        throw away live work.
        """
        try:
            pods = self.api.list("pods", self._run_selector(handle))
        except Exception:
            return "unknown"
        if not pods:
            return "queued"
        for pod in pods:
            if ((pod.get("spec") or {}).get("nodeName") or "").strip():
                return "placed"
        return "queued"

    def environment(self, handle: DeploymentHandle) -> dict[str, Any]:
        """Image + node the workload landed on, for provenance. Best-effort —
        a run must never fail because this could not be read."""
        snapshot: dict[str, Any] = {}
        try:
            resource = "job" if handle.workload else self._resource
            primary = self.api.get(resource, handle.container_name) or {}
            if handle.workload or self._mode == "deployment":
                # Both a Job and a Deployment carry a pod template; only the CR
                # puts the image at the top of its spec.
                containers = (
                    ((primary.get("spec") or {}).get("template") or {}).get("spec") or {}
                ).get("containers") or []
                image = containers[0].get("image") if containers else None
            else:
                image = (primary.get("spec") or {}).get("image")
            if image:
                snapshot["image_tag"] = image
            pods = self.api.list("pods", self._run_selector(handle))
            if pods:
                node = (pods[0].get("spec") or {}).get("nodeName")
                if node:
                    snapshot["k8s_node"] = node
                    # The card type of the node the pod ACTUALLY landed on — the
                    # ground truth for comparison, more reliable than the
                    # machine's declared type once a machine can span a node pool.
                    card_type = self._node_card_type(node)
                    if card_type:
                        snapshot["card_type"] = card_type
        except Exception as exc:
            logger.warning("k8s environment snapshot for %s failed: %s", handle.container_name, exc)
        return {k: v for k, v in snapshot.items() if v}

    # -- generic workloads (policy containers) --------------------------------

    def launch_workload(self, spec: WorkloadSpec) -> tuple[DeploymentHandle, str]:
        job = render_policy_job(spec, self.settings)
        # apply, not create: same idempotence as the ssh driver's `docker rm -f`
        # before `docker run` — a worker that died between launching and
        # committing relaunches safely onto the same name.
        self.api.apply(job)
        handle = DeploymentHandle(
            driver=self.name,
            container_name=spec.name,
            machine=spec.machine,
            endpoint_url="",  # a workload serves no endpoint the platform probes
            workload=True,
        )
        return handle, _yaml_ish([job])

    def workload_state(self, handle: DeploymentHandle) -> WorkloadState:
        """RUNNING / EXITED / GONE from the Job's own status.

        Mirrors the ssh driver's `docker inspect` exactly: a live container is
        RUNNING, a container that finished either way is EXITED, and an object
        that is not there at all is GONE. `active` is the pod count still
        running; `succeeded`/`failed` are terminal. A Job that exists but has
        not yet made a pod is still coming up, so it reads RUNNING — GONE ends
        the session, and losing one to a scheduling delay would be a lie.
        """
        job = self.api.get("job", handle.container_name)
        if job is None:
            return WorkloadState.GONE
        status = job.get("status") or {}
        if int(status.get("active") or 0) > 0:
            return WorkloadState.RUNNING
        if int(status.get("succeeded") or 0) or int(status.get("failed") or 0):
            return WorkloadState.EXITED
        return WorkloadState.RUNNING

    # -- baseline lifecycle (cluster-managed; nothing for us to do) -----------
    #
    # On bare metal the platform borrows a box and must stop/restore the
    # production services on it. On k8s it borrows GPU *quota*: the cluster
    # keeps production running elsewhere and schedules our workloads on whatever
    # it frees. So there is nothing to capture, clear or restore — and returning
    # an empty capture is exactly right: the supervisor reads "no production
    # services to stop" as "the pool is already ours" and proceeds, while the
    # nightly baseline is measured by relaunching the production config.

    def capture_baseline(self, machine: MachineInfo) -> dict:
        return {
            "driver": self.name,
            "services": [],
            "note": "k8s pool: production is cluster-managed and is not stopped by the platform",
        }

    def clear_baseline(self, machine: MachineInfo, baseline: dict) -> list[str]:
        return []

    def restore_baseline(self, machine: MachineInfo, baseline: dict) -> list[str]:
        return []

    def verify_baseline(self, machine: MachineInfo, baseline: dict) -> list[dict]:
        return []

    # -- helpers -------------------------------------------------------------

    def _resolve_endpoint(self, spec: LaunchSpec) -> str:
        if self._mode == "deployment":
            return self._deployment_endpoint(spec)
        cr = self.api.get(self._resource, spec.container_name)
        return _endpoint_from_cr(cr, spec)

    def _deployment_endpoint(self, spec: LaunchSpec) -> str:
        """`http://<node>:<nodePort>` — the NodePort Service reached via a node
        IP. Falls back to the spec's host:port until the Service has a port
        (right after apply) or if no node IP can be read."""
        port = self.settings.k8s_service_nodeport
        if not port:
            svc = self.api.get("service", spec.container_name)
            ports = ((svc or {}).get("spec") or {}).get("ports") or []
            port = ports[0].get("nodePort") if ports else None
        host = self.settings.k8s_node_host or self._first_node_ip()
        if host and port:
            return f"http://{host}:{port}"
        return spec.endpoint_url

    def _first_node_ip(self) -> str:
        try:
            nodes = self.api.list("nodes")
        except Exception:
            return ""
        for node in nodes:
            for addr in (node.get("status") or {}).get("addresses") or []:
                if addr.get("type") == "InternalIP":
                    return str(addr.get("address") or "")
        return ""

    # -- capacity ------------------------------------------------------------

    def probe_capacity(self, machine: MachineInfo) -> dict[str, Any]:
        """Read the node(s) this machine's selector matches and report GPU count
        and card type from the cluster — so a k8s machine's capacity is the
        cluster's truth, not a number someone typed.

        The machine's `node_selector` ("k=v,k2=v2") is already a valid k8s label
        selector, so it filters the node list directly. A selector may match one
        node (a hostname pin) or many (a `nvidia.com/gpu.product=...` type pool):
        the count is summed across matched nodes, and the type is the one they
        share — a selector that spans two card types is a mistake worth a loud
        warning, not a silently-averaged number.
        """
        selector = machine.node_selector or self.settings.k8s_node_selector
        warnings: list[str] = []
        if not selector.strip():
            return {
                "supported": True, "gpu_count": 0, "gpu_type": "", "node_count": 0,
                "nodes": [],
                "warnings": ["no node selector set — cannot tell which nodes this "
                             "machine is; pin a hostname or a GPU-type label"],
            }
        try:
            nodes = self.api.list("nodes", label_selector=selector.strip())
        except Exception as exc:
            return {"supported": True, "gpu_count": 0, "gpu_type": "", "node_count": 0,
                    "nodes": [], "warnings": [f"could not read nodes: {exc}"]}

        detail: list[dict[str, Any]] = []
        total = 0
        types: set[str] = set()
        for node in nodes:
            count = _node_gpu_count(node)
            gpu_type = _node_gpu_type(node)
            total += count
            if gpu_type:
                types.add(gpu_type)
            else:
                labels = (node.get("metadata") or {}).get("labels") or {}
                raw = labels.get(_GPU_PRODUCT_LABEL)
                warnings.append(
                    f"{_node_name(node)}: unknown GPU product {raw!r} — add it to "
                    "app.hardware.GPU_TYPES" if raw
                    else f"{_node_name(node)}: no GPU product label"
                )
            detail.append({"node": _node_name(node), "gpu_count": count, "gpu_type": gpu_type})

        if not nodes:
            warnings.append(f"selector {selector!r} matched no nodes")
        if len(types) > 1:
            warnings.append(
                f"selector spans multiple card types {sorted(types)} — results "
                "across them are not comparable; pin one type or one node"
            )
        return {
            "supported": True,
            "gpu_count": total,
            "gpu_type": next(iter(types)) if len(types) == 1 else "",
            "node_count": len(nodes),
            "nodes": detail,
            "warnings": warnings,
        }

    def _node_card_type(self, node_name: str) -> str:
        """The canonical card type of one node by name, for run provenance."""
        if not node_name:
            return ""
        try:
            nodes = self.api.list("nodes", label_selector=f"kubernetes.io/hostname={node_name}")
        except Exception:
            return ""
        for node in nodes:
            if _node_name(node) == node_name or len(nodes) == 1:
                return _node_gpu_type(node)
        return ""


def _yaml_ish(manifests: list[dict]) -> str:
    """A readable rendering of the applied manifests for the launch-command
    record. Not a YAML serializer — JSON is valid YAML — just indented so the
    stored command reads like what an operator would apply by hand."""
    return json.dumps(manifests, indent=2, sort_keys=True)
