"""ssh + docker run driver — the PoC deployment substrate.

Mirrors the conventions of the production deploy scripts
(inference/deploy-scripts/deploy_h200.sh) so autotune launches behave like
manual launches:

- model dir is bind-mounted to /model and the engine gets --model-path /model
- per-run host cache dir mounted at /root/.cache
- --ipc=host + memlock/stack ulimits + SYS_PTRACE/SYS_NICE caps,
  NVIDIA_DISABLE_REQUIRE=1, PYTORCH_ALLOC_CONF=expandable_segments:True
- pre-launch port check (ss -lntp) — a busy port is a distinct failure class
- container logs are ALWAYS dumped on the remote host before `docker rm -f`
  (preserve the crash scene; keep the most recent 20 dumps)

Uses the system `ssh` binary (inherits ~/.ssh/config, agent, ProxyJump) via
subprocess. All state lives in docker itself (container name =
autotune-run-<id>), which is what makes attach()/teardown() safe across worker
restarts. Container names deliberately do NOT start with "sglang"/"vllm" so
production cleanup scripts never mistake autotune containers for theirs.
"""

import json
import logging
import shlex
import subprocess
import time

import httpx

from app.control.engine_command import PLACEMENT_FLAGS, parse_engine_args
from app.control.engines import MODEL_MOUNT, get_adapter
from app.control.launch.base import (
    DeploymentDriver,
    DeploymentHandle,
    DeploymentState,
    LaunchSpec,
    WorkloadSpec,
    WorkloadState,
    merge_volumes,
)
from app.control.launch.failures import classify_exit, classify_failure
from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Imported, not redeclared: the adapter passes `--model-path /model` and this
# driver is what puts the weights there. Two copies of the constant would let
# the mount and the flag drift apart, which fails as "model not found" at
# launch with nothing in the config to explain it.
REMOTE_BASE = "/root/deploy/autotune"
REMOTE_LOG_DIR = f"{REMOTE_BASE}/log"
KEEP_LOG_DUMPS = 20
# A free card idles near zero; a loaded model holds gigabytes. The threshold
# only needs to clear driver/context overhead, not distinguish model sizes, so
# it is deliberately generous — a card over 1 GiB is doing something.
GPU_BUSY_MB = 1024
# Their convention: one deploy script per served port, under /root/deploy.
BASELINE_SCRIPT_GLOB = "/root/deploy/*.sh"
# How long the master may take to open its rendezvous socket before we call the
# launch wedged. Generous: it is a container start plus an import, not a model
# load, so a master that has not bound in three minutes is not slow, it is stuck
# — and every second spent waiting is a second the workers are not running.
RENDEZVOUS_TIMEOUT_SECONDS = 180
RENDEZVOUS_POLL_SECONDS = 2


def _inspect_to_spec(inspect_json: str) -> dict:
    """Pull the fields needed to rebuild a container out of `docker inspect`."""
    try:
        data = json.loads(inspect_json)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(data, list) or not data:
        return {}
    container = data[0]
    config = container.get("Config") or {}
    host = container.get("HostConfig") or {}
    device_requests = host.get("DeviceRequests") or []
    gpu_ids: list[str] = []
    for request in device_requests:
        for capability in request.get("DeviceIDs") or []:
            gpu_ids.append(str(capability))
    return {
        "image": config.get("Image", ""),
        "cmd": config.get("Cmd") or [],
        "entrypoint": config.get("Entrypoint") or [],
        "env": [e for e in (config.get("Env") or []) if not _is_default_env(e)],
        "binds": host.get("Binds") or [],
        "network_mode": host.get("NetworkMode", ""),
        "ipc_mode": host.get("IpcMode", ""),
        "runtime": host.get("Runtime", ""),
        "cap_add": host.get("CapAdd") or [],
        "security_opt": host.get("SecurityOpt") or [],
        "ulimits": host.get("Ulimits") or [],
        "gpu_count": (device_requests[0].get("Count") if device_requests else 0) or 0,
        "gpu_ids": gpu_ids,
        "restart_policy": (host.get("RestartPolicy") or {}).get("Name", ""),
    }


# Docker injects these into every container; replaying them is noise.
_DEFAULT_ENV_PREFIXES = ("PATH=", "HOSTNAME=", "HOME=", "TERM=")


def _is_default_env(entry: str) -> bool:
    return entry.startswith(_DEFAULT_ENV_PREFIXES)


def _render_restore_command(name: str, spec: dict) -> str:
    """Rebuild the `docker run` that recreates this container.

    Used when no deploy script is recorded for a service — otherwise it has no
    restore path at all, which is worse than an imperfect reconstruction.
    """
    if not spec.get("image"):
        return ""
    parts: list[str] = ["docker", "run", "-d", "--name", name]
    if spec.get("network_mode"):
        parts += ["--network", spec["network_mode"]]
    if spec.get("ipc_mode"):
        parts += ["--ipc", spec["ipc_mode"]]
    if spec.get("runtime"):
        parts += ["--runtime", spec["runtime"]]
    if spec.get("gpu_ids"):
        parts += ["--gpus", f'"device={",".join(spec["gpu_ids"])}"']
    elif spec.get("gpu_count"):
        parts += ["--gpus", "all" if spec["gpu_count"] in (-1, 0) else str(spec["gpu_count"])]
    for cap in spec.get("cap_add") or []:
        parts += ["--cap-add", cap]
    for opt in spec.get("security_opt") or []:
        parts += ["--security-opt", opt]
    for limit in spec.get("ulimits") or []:
        soft = limit.get("Soft", 0)
        parts += ["--ulimit", f"{limit.get('Name')}={soft}"]
    for env in spec.get("env") or []:
        parts += ["-e", env]
    for bind in spec.get("binds") or []:
        parts += ["-v", bind]
    if spec.get("restart_policy") and spec["restart_policy"] != "no":
        parts += ["--restart", spec["restart_policy"]]
    entrypoint = spec.get("entrypoint") or []
    if entrypoint:
        parts += ["--entrypoint", entrypoint[0]]
    parts.append(spec["image"])
    parts += entrypoint[1:]
    parts += spec.get("cmd") or []
    return " ".join(shlex.quote(p) if " " in p and not p.startswith('"') else p for p in parts)


def _int_or(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


DEFAULT_ENV = {
    "NVIDIA_DISABLE_REQUIRE": "1",
    "PYTORCH_ALLOC_CONF": "expandable_segments:True",
}

# Run inside the serving container to record what the numbers were produced
# by. Every lookup is individually guarded: this must never raise, because a
# container that answers nothing should still leave us the image digest, and
# no version string is worth interrupting a benchmark for. Prints one JSON
# line, which is what the caller parses.
_VERSION_PROBE = """
import json
def _v(mod, attr="__version__"):
    try:
        return str(getattr(__import__(mod), attr))
    except Exception:
        return ""
out = {"engine_version": _v("sglang") or _v("vllm"), "torch_version": _v("torch")}
try:
    import torch
    out["cuda_version"] = str(torch.version.cuda or "")
    out["gpu_name"] = torch.cuda.get_device_name(0) if torch.cuda.is_available() else ""
except Exception:
    pass
print(json.dumps({k: v for k, v in out.items() if v}))
"""


def render_engine_command(spec: LaunchSpec) -> list[str]:
    """Full in-container server command; element 0 becomes the docker entrypoint.

    The per-engine argv now lives in `app.control.engines` — how a model is
    served is engine knowledge, and the k8s driver needs the identical answer.
    """
    return get_adapter(spec.engine).build_command(spec)


def _docker_base(
    name: str,
    machine,
    gpu_indices: list[int] | None,
    env: dict[str, str],
    volumes: dict[str, str],
) -> list[str]:
    """The docker-run head shared by engines and generic workloads: everything
    up to (not including) the image. gpu_indices=None means "no GPUs at all" —
    a delegated-only policy container never touches a card — while [] means
    "all of them", matching LaunchSpec's convention."""
    cmd = [
        "docker", "run", "-d",
        "--name", name,
        "--network", "host",
    ]
    # GPU-ness is a property of the MACHINE, not of the platform: the same
    # worker drives a CPU-only test box and an 8×A100 node. A global "none"
    # setting once launched sglang with no accelerator on real hardware.
    gpu_mode = get_settings().docker_gpu_mode
    machine_has_gpus = (machine.gpu_count or 0) > 0
    if gpu_mode != "none" and machine_has_gpus and gpu_indices is not None:
        gpus = (
            f'"device={",".join(str(i) for i in gpu_indices)}"'
            if gpu_indices
            else "all"
        )
        cmd += ["--gpus", gpus, "--runtime=nvidia"]
    cmd += [
        "--ipc=host",
        "--cap-add=SYS_PTRACE",
        "--cap-add=SYS_NICE",
        "--ulimit", "memlock=-1",
        "--ulimit", "stack=67108864",
        "--security-opt", "label=disable",  # parity with the prod deploy scripts
        "--restart", "no",
    ]
    for key, value in sorted({**DEFAULT_ENV, **env}.items()):
        cmd += ["-e", f"{key}={value}"]
    for host_path, container_path in sorted(volumes.items()):
        cmd += ["-v", f"{host_path}:{container_path}"]
    return cmd


def render_docker_command(spec: LaunchSpec) -> list[str]:
    engine_cmd = render_engine_command(spec)
    cache_dir = f"{REMOTE_BASE}/run-{spec.run_id}/cache"
    volumes = merge_volumes(
        {spec.model_path: MODEL_MOUNT, cache_dir: "/root/.cache"}, spec.volumes
    )
    cmd = _docker_base(
        spec.container_name, spec.machine, spec.gpu_indices, spec.env, volumes
    )
    cmd += ["--entrypoint", engine_cmd[0], spec.image]
    cmd += engine_cmd[1:]
    return cmd


def render_workload_command(spec: WorkloadSpec) -> list[str]:
    """A generic container: the image's own entrypoint unless a command is
    given, no model mount, no cache, no implied readiness."""
    cmd = _docker_base(spec.name, spec.machine, spec.gpu_indices, spec.env, spec.volumes)
    if spec.command:
        cmd += ["--entrypoint", spec.command[0], spec.image]
        cmd += spec.command[1:]
    else:
        cmd += [spec.image]
    return cmd


class SshDockerDriver(DeploymentDriver):
    name = "ssh_docker"

    def _ssh(
        self, machine, remote_cmd: str, timeout: int = 60
    ) -> subprocess.CompletedProcess:
        settings = get_settings()
        ssh_cmd = [
            "ssh",
            "-o", "BatchMode=yes",
            # Host keys are not checked. The fleet is an internal network of
            # boxes that get re-imaged and re-addressed, so first contact with
            # a new machine would otherwise be a hard failure (BatchMode turns
            # the interactive prompt into one) and a re-imaged box would fail
            # the same way until someone edited known_hosts by hand — which is
            # exactly how a leased machine silently stalled every submission
            # queued for it. The trade is real and deliberate: a swapped host
            # key on the path to a GPU box will not be noticed here.
            #
            # /dev/null rather than the default file because $HOME/.ssh is
            # mounted READ-ONLY into these containers: ssh would try to record
            # each new key, fail, and print a warning that the driver folds
            # into the error message of every first call to a machine. It also
            # makes the setting mean what it says — with a real known_hosts, a
            # CHANGED key still refuses even under StrictHostKeyChecking=no.
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            # Without this, every connection prints "Warning: Permanently added
            # …" to stderr, and stderr is what a failed launch reports.
            "-o", "LogLevel=ERROR",
            "-o", f"ConnectTimeout={settings.ssh_connect_timeout}",
            "-o", "ServerAliveInterval=30",
            "-p", str(machine.ssh_port),
            f"{machine.ssh_user}@{machine.host}",
            remote_cmd,
        ]
        try:
            return subprocess.run(
                ssh_cmd, capture_output=True, text=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired as exc:
            # Surface as a RuntimeError so callers classify it as a launch
            # failure instead of it escaping as an unexpected supervisor error.
            raise RuntimeError(
                f"ssh to {machine.host} timed out after {timeout}s: {remote_cmd[:200]}"
            ) from exc

    @staticmethod
    def _q(parts: list[str]) -> str:
        return " ".join(shlex.quote(part) for part in parts)

    # -- DeploymentDriver ----------------------------------------------------

    def launch(self, spec: LaunchSpec) -> tuple[DeploymentHandle, str]:
        self._check_port_free(spec.machine, spec.port)
        # Physical GPU backstop: the scheduler picked these cards believing them
        # free, but a previous run's container may not have finished releasing
        # them. Refuse rather than crash three minutes into a model load on a
        # busy card. Skipped when THIS run already has a container here (an
        # idempotent relaunch reuses its own cards; its residual memory is not a
        # collision).
        if self.attach(spec) is None:
            busy = self.gpus_in_use(spec.machine, spec.gpu_indices)
            if busy:
                raise RuntimeError(
                    f"gpu busy on {spec.machine.host}: cards {busy} still hold memory "
                    "(a previous container has not finished releasing them)"
                )
        # Idempotent relaunch: dump + remove any stale container from a
        # previous attempt of this same run before starting fresh.
        self._dump_remote_logs(spec.machine, spec.container_name)
        self._ssh(spec.machine, f"docker rm -f {spec.container_name} 2>/dev/null || true")

        docker_cmd = render_docker_command(spec)
        cache_dir = f"{REMOTE_BASE}/run-{spec.run_id}/cache"
        launch_command = self._q(docker_cmd)
        result = self._ssh(
            spec.machine, f"mkdir -p {shlex.quote(cache_dir)} && {launch_command}", timeout=180
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"docker run failed on {spec.machine.host}: {result.stderr.strip()[:2000]}"
            )
        handle = DeploymentHandle(
            driver=self.name,
            container_name=spec.container_name,
            machine=spec.machine,
            endpoint_url=spec.endpoint_url,
        )
        return handle, launch_command

    def wait_rendezvous(self, spec: LaunchSpec) -> None:
        """Block until the master is listening for its workers.

        sglang's rank 0 binds the dist-init socket and THEN blocks for peers, so
        the socket being open is the right signal — it does not wait for the
        model to load, which is the whole point of launching the workers now
        rather than after a readiness poll. Starting a worker before this socket
        exists is a race the worker usually loses.
        """
        if spec.rank != 0 or not spec.multi_node or not spec.dist_port:
            return
        deadline = time.monotonic() + RENDEZVOUS_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._port_listening(spec.machine, spec.dist_port):
                return
            time.sleep(RENDEZVOUS_POLL_SECONDS)
        raise RuntimeError(
            f"dist_init_timeout: master {spec.machine.host} never opened its rendezvous "
            f"port {spec.dist_port} within {RENDEZVOUS_TIMEOUT_SECONDS}s "
            f"({spec.container_name})"
        )

    def _port_listening(self, machine, port: int) -> bool:
        """Is anything on this host listening on `port` right now? `--network
        host` puts the container's socket in the host namespace, so `ss` sees it."""
        result = self._ssh(
            machine,
            f"ss -lnt 2>/dev/null | awk '{{print $4}}' | grep -q ':{int(port)}$'",
            timeout=30,
        )
        return result.returncode == 0

    def _container_state(self, handle: DeploymentHandle) -> DeploymentState:
        """What docker alone says about one container: up, exited, or absent.
        No endpoint probe — the master's is a separate question, and a worker's
        is not a question at all (workers serve no HTTP API)."""
        result = self._ssh(
            handle.machine,
            self._q(["docker", "inspect", "-f", "{{.State.Status}}", handle.container_name]),
        )
        if result.returncode != 0:
            return DeploymentState.GONE
        return (
            DeploymentState.STARTING
            if result.stdout.strip() == "running"
            else DeploymentState.CRASHED
        )

    def _node_state(self, handle: DeploymentHandle) -> DeploymentState:
        """One node's state. Only rank 0's endpoint is probed; a gang is ready
        when its master answers, which for sglang means every rank has joined."""
        state = self._container_state(handle)
        if state != DeploymentState.STARTING:
            return state
        if handle.rank == 0 and self._endpoint_ready(handle.endpoint_url):
            return DeploymentState.READY
        return DeploymentState.STARTING

    def state(self, handle: DeploymentHandle) -> DeploymentState:
        """The gang's state: a dead worker is a dead deployment.

        Without this a worker that vanished reads as "still starting" until the
        ready timeout, and the run fails with the one class that means "we do not
        know" instead of the one that says a node is gone.
        """
        if not handle.node_handles:
            return self._node_state(handle)
        worker_states = [self._node_state(node) for node in handle.node_handles]
        if any(s in (DeploymentState.CRASHED, DeploymentState.GONE) for s in worker_states):
            return DeploymentState.CRASHED
        return self._node_state(handle)

    def _logs_one(self, handle: DeploymentHandle, tail: int) -> str:
        result = self._ssh(
            handle.machine,
            self._q(["docker", "logs", "--tail", str(tail), handle.container_name]),
            timeout=60,
        )
        # docker logs writes to both streams
        return (result.stdout + "\n" + result.stderr).strip()

    def logs(self, handle: DeploymentHandle, tail: int = 200) -> str:
        """The master's log, or every rank's with a header when there is a gang.

        A multi-node failure is usually explained by ONE rank's log and nobody
        knows which in advance, so capturing only the master's would throw away
        the evidence at exactly the moment it is destroyed (teardown removes
        every container).
        """
        if not handle.node_handles:
            return self._logs_one(handle, tail)
        parts: list[str] = []
        for node in handle.all_handles:
            role = "master" if node.rank == 0 else f"worker rank {node.rank}"
            parts.append(f"===== {role} · {node.machine.name} ({node.container_name}) =====")
            parts.append(self._logs_one(node, tail))
        return "\n".join(parts).strip()

    def request_stop(self, handle: DeploymentHandle) -> None:
        # SIGTERM only, and return at once: the engine gets to flush GPU memory
        # and tear down NCCL on its own clock. The forced `docker rm -f` is the
        # janitor's escalation once the grace elapses. `|| true` because a
        # container that already exited is not an error to signal.
        #
        # Workers first: a master that exits drops the rendezvous the workers are
        # still joined to, which turns a clean shutdown into a crash on their side.
        for node in reversed(handle.all_handles):
            self._ssh(
                node.machine,
                f"docker kill --signal=SIGTERM {shlex.quote(node.container_name)} "
                "2>/dev/null || true",
                timeout=30,
            )

    def _teardown_one(self, handle: DeploymentHandle) -> None:
        # Dump before rm — `docker rm -f` permanently destroys the crash scene
        # otherwise (hard-won lesson from the production deploy scripts).
        self._dump_remote_logs(handle.machine, handle.container_name)
        result = self._ssh(
            handle.machine, f"docker rm -f {handle.container_name}", timeout=120
        )
        if result.returncode != 0 and "No such container" not in result.stderr:
            logger.warning(
                "teardown of %s on %s: %s",
                handle.container_name,
                handle.machine.host,
                result.stderr.strip()[:500],
            )

    def teardown(self, handle: DeploymentHandle) -> None:
        """Remove every node, workers first. Idempotent per node, so a
        half-dead gang is fully removable and a retry is harmless."""
        for node in reversed(handle.all_handles):
            self._teardown_one(node)

    def is_gone(self, handle: DeploymentHandle) -> bool:
        """Confirmed absent only when EVERY node is. A gang whose master is gone
        but whose worker still holds cards must keep those cards reserved."""
        return all(self._node_state(node) == DeploymentState.GONE for node in handle.all_handles)

    def gpus_in_use(self, machine, indices: list[int]) -> list[int]:
        """The subset of `indices` whose cards are holding meaningful memory
        right now. `nvidia-smi` is the source of truth the run ledger cannot
        see: a card a dying container still pins reads busy here even though no
        live run row claims it. Any read failure (no nvidia-smi, an ssh blip)
        returns none-busy — the probe is a backstop, never a new way for a
        launch to fail."""
        if not indices:
            return []
        result = self._ssh(
            machine,
            "nvidia-smi --query-gpu=index,memory.used --format=csv,noheader,nounits",
            timeout=30,
        )
        if result.returncode != 0:
            return []
        want = set(indices)
        busy: list[int] = []
        for line in result.stdout.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) != 2:
                continue
            try:
                idx, used_mb = int(parts[0]), int(float(parts[1]))
            except ValueError:
                continue
            if idx in want and used_mb >= GPU_BUSY_MB:
                busy.append(idx)
        return sorted(busy)

    def attach(self, spec: LaunchSpec) -> DeploymentHandle | None:
        """Re-find the deployment from the spec's plan. Never relaunches, which
        is what makes a worker restart safe: every container name is
        deterministic (the run row plus the rank), so nothing has to survive in
        memory for the gang to be found again."""
        if spec.nodes and len(spec.nodes) > 1:
            handles = [
                DeploymentHandle(
                    driver=self.name,
                    container_name=spec.for_node(node).container_name,
                    machine=node.machine,
                    endpoint_url=spec.for_node(node).endpoint_url,
                    rank=node.rank,
                )
                for node in sorted(spec.nodes, key=lambda n: n.rank)
            ]
            master = handles[0].model_copy(update={"node_handles": handles[1:]})
            # "Still there" means the master is; a worker that is gone is a
            # crash the state machine reports, not a reason to say the run
            # vanished entirely (which would lose the master's log).
            if self._node_state(handles[0]) == DeploymentState.GONE:
                return None
            return master
        handle = DeploymentHandle(
            driver=self.name,
            container_name=spec.container_name,
            machine=spec.machine,
            endpoint_url=spec.endpoint_url,
        )
        return handle if self.state(handle) != DeploymentState.GONE else None

    def failure_reason(self, handle: DeploymentHandle) -> tuple[str, str] | None:
        """Name the node that is not running, when that is what wedged the run.

        A worker that never joined leaves the master sitting in rendezvous with a
        log that says nothing about why, so the substrate's own view — which
        container is not up — is the only honest answer."""
        if not handle.node_handles:
            return None
        for node in handle.all_handles:
            state = self._container_state(node)
            if state in (DeploymentState.CRASHED, DeploymentState.GONE):
                role = "master" if node.rank == 0 else f"worker rank {node.rank}"
                return (
                    "node_failed",
                    f"{role} ({node.container_name}) on {node.machine.name} is "
                    f"{state.value}; the deployment cannot rendezvous",
                )
        return None

    def exit_info(self, handle: DeploymentHandle) -> tuple[int | None, bool]:
        """The first node that actually exited with a code. A gang's story is
        whichever rank died first, not the master's absent exit status."""
        for node in handle.all_handles:
            code, oom = self._exit_info_one(node)
            if code is not None:
                return code, oom
        return (None, False)

    def _exit_info_one(self, handle: DeploymentHandle) -> tuple[int | None, bool]:
        result = self._ssh(
            handle.machine,
            self._q([
                "docker", "inspect", "-f",
                "{{.State.ExitCode}} {{.State.OOMKilled}}", handle.container_name,
            ]),
            timeout=30,
        )
        if result.returncode != 0:
            return (None, False)
        parts = result.stdout.strip().split()
        if len(parts) != 2:
            return (None, False)
        try:
            return (int(parts[0]), parts[1].lower() == "true")
        except ValueError:
            return (None, False)

    # -- generic workloads (policy containers) --------------------------------

    def launch_workload(self, spec: WorkloadSpec) -> tuple[DeploymentHandle, str]:
        # A leftover engine from last night squatting on a session port must
        # fail the session loudly NOW, not as a bind error inside the policy
        # three hours in.
        for port in spec.ports:
            self._check_port_free(spec.machine, port)
        # Same idempotence as launch(): dump + remove any stale container from
        # a previous attempt (a worker that crashed between docker run and the
        # DB commit relaunches safely).
        self._dump_remote_logs(spec.machine, spec.name)
        self._ssh(spec.machine, f"docker rm -f {spec.name} 2>/dev/null || true")

        launch_command = self._q(render_workload_command(spec))
        result = self._ssh(spec.machine, launch_command, timeout=180)
        if result.returncode != 0:
            raise RuntimeError(
                f"docker run failed on {spec.machine.host}: {result.stderr.strip()[:2000]}"
            )
        handle = DeploymentHandle(
            driver=self.name,
            container_name=spec.name,
            machine=spec.machine,
            endpoint_url="",  # a workload serves no endpoint the platform probes
        )
        return handle, launch_command

    def workload_state(self, handle: DeploymentHandle) -> WorkloadState:
        result = self._ssh(
            handle.machine,
            self._q(["docker", "inspect", "-f", "{{.State.Status}}", handle.container_name]),
        )
        if result.returncode != 0:
            return WorkloadState.GONE
        return (
            WorkloadState.RUNNING
            if result.stdout.strip() == "running"
            else WorkloadState.EXITED
        )

    # -- baseline lifecycle --------------------------------------------------

    def capture_baseline(self, machine) -> dict:
        """Inventory the production containers and pair each with the deploy
        script that can recreate it.

        Their convention (inference/deploy-scripts, and /root/deploy on the
        nodes) is one script per port, named deploy-<port>.sh — so the port
        parsed out of a container's command identifies its restore script.
        """
        listing = self._ssh(machine, "docker ps --format '{{.Names}}\\t{{.Image}}'")
        if listing.returncode != 0:
            raise RuntimeError(f"docker ps failed on {machine.host}: {listing.stderr[:300]}")

        scripts = self._ssh(machine, f"ls -1 {BASELINE_SCRIPT_GLOB} 2>/dev/null || true")
        script_paths = [line.strip() for line in scripts.stdout.splitlines() if line.strip()]

        services: list[dict] = []
        for line in listing.stdout.splitlines():
            if not line.strip():
                continue
            name, _, image = line.partition("\t")
            name, image = name.strip(), image.strip()
            # `autotune-` covers runs AND policy containers: a leftover
            # autotune-policy-N captured here would be "restored" as production
            # on hand-back, which is the exact disaster capture exists to
            # prevent.
            if name.startswith("autotune-"):
                continue  # ours, not production
            # Full inspect, not just the command: a restore script can be
            # stale or missing entirely, and only the container itself is
            # ground truth for what was actually deployed.
            full = self._ssh(
                machine, self._q(["docker", "inspect", name]), timeout=60
            )
            spec = _inspect_to_spec(full.stdout)
            command = json.dumps(spec.get("cmd", []))
            # Parse the command once, the same way parity and the canary do, so
            # production's config is captured as engine args — unified with a
            # search config — rather than re-scanned flag by flag.
            args = parse_engine_args(command)
            engine_args = {k: v for k, v in args.items() if k not in PLACEMENT_FLAGS}
            port = str(args.get("port", "") or "")
            served = str(args.get("served_model_name", "") or "")
            restore_script = next(
                (path for path in script_paths if port and port in path), ""
            )
            # GPUs this service occupies — needed to compare it fairly against
            # experiment configs on card-normalized metrics, and to detect a
            # parallelism change on restore.
            tp = _int_or(str(args.get("tp") or args.get("tp_size") or 1), 1)
            dp = _int_or(str(args.get("dp") or args.get("dp_size") or 1), 1)
            pp = _int_or(str(args.get("pp") or args.get("pp_size") or 1), 1)
            services.append({
                "container": name,
                "image": image,
                "port": port,
                "served_model_name": served,
                "endpoint_url": f"http://{machine.host}:{port}" if port else "",
                "restore_script": restore_script,
                # Production's real engine config, in the same shape a search
                # candidate carries — so the canary, parity and card-norm all
                # read it identically.
                "engine_args": engine_args,
                "cards": tp * dp * pp,
                "command": command[:4000],
                # Everything needed to rebuild this container ourselves when
                # no script exists (or the script is not trusted).
                "docker_run": _render_restore_command(name, spec),
            })
        # Lowest port first: deterministic, and the primary service by convention.
        services.sort(key=lambda s: _int_or(s["port"], 99999))
        return {
            "driver": self.name,
            "services": services,
            "restore_scripts": script_paths,
        }

    def clear_baseline(self, machine, baseline: dict) -> list[str]:
        stopped = []
        for service in baseline.get("services", []):
            container = service["container"]
            self._dump_remote_logs(machine, container)  # never destroy the scene
            result = self._ssh(machine, f"docker rm -f {shlex.quote(container)}", timeout=180)
            if result.returncode == 0 or "No such container" in result.stderr:
                stopped.append(container)
            else:
                raise RuntimeError(
                    f"failed to stop {container} on {machine.host}: {result.stderr[:300]}"
                )
        return stopped

    def _service_already_up(self, machine, service: dict) -> str:
        """A live service restore would collide with, described, or "".

        Don't kill what we did not start. Restore exists to bring production
        BACK, not to insist it be our copy — so if the service is already
        answering, or a container is already running under the captured name,
        someone (the operator, by hand) restored it and restore must be a
        no-op. The one authorised teardown of a service we did not start is the
        hand-over clear at campaign start; every path after it, restore
        included, leaves a running service alone.

        Seen live: an operator restored production by hand while
        a campaign was paused; the lease later expired and restore ran
        `docker rm -f` on the captured name, tearing their live service down to
        relaunch ours. That is the stomp this guard prevents.
        """
        endpoint = service.get("endpoint_url")
        if endpoint and self._endpoint_ready(endpoint):
            return f"already answering at {endpoint}"
        container = service.get("container")
        if container:
            probe = self._ssh(
                machine,
                f"docker ps --filter {shlex.quote('name=^' + container + '$')} "
                "--format '{{.Names}}'",
                timeout=60,
            )
            if probe.returncode == 0 and container in probe.stdout.split():
                return f"container {container} already running"
        return ""

    def restore_baseline(self, machine, baseline: dict) -> list[str]:
        """Prefer the operator's deploy script — it is the mechanism they own
        and trust. Fall back to replaying the captured `docker run` when no
        script is recorded, so a hand-started service still comes back.

        Whichever path, a service that is ALREADY up is left untouched: we do
        not tear down and relaunch a container we did not start (see
        `_service_already_up`).
        """
        restored, errors = [], []
        for service in baseline.get("services", []):
            container = service["container"]
            already = self._service_already_up(machine, service)
            if already:
                restored.append(container)
                logger.info(
                    "%s on %s: %s — restore is a no-op, leaving it as is",
                    container, machine.host, already,
                )
                continue
            script = service.get("restore_script")
            if script:
                command, how = f"bash {shlex.quote(script)}", f"script {script}"
            elif service.get("docker_run"):
                # Recreating by name requires the old container to be gone.
                command = (
                    f"docker rm -f {shlex.quote(container)} >/dev/null 2>&1; "
                    + service["docker_run"]
                )
                how = "captured docker run"
            else:
                errors.append(f"{container}: nothing recorded to restore it with")
                continue

            result = self._ssh(machine, command, timeout=900)
            if result.returncode == 0:
                restored.append(container)
                logger.info("restored %s on %s via %s", container, machine.host, how)
            else:
                errors.append(f"{container} (via {how}): {result.stderr.strip()[:300]}")
        if errors:
            raise RuntimeError("; ".join(errors))
        return restored

    def verify_baseline(self, machine, baseline: dict) -> list[dict]:
        """Compare what is running now against what we captured.

        A restore script can silently disagree with the container it was
        supposed to recreate — observed live: deploy-8050.sh declared --tp 2
        while the running service was tp=4, so "restoring" quietly halved that
        deployment. Restoring is not the same as restoring *faithfully*, and
        only the captured command can tell the difference.
        """
        current = {s["port"]: s for s in self.capture_baseline(machine).get("services", [])}
        findings: list[dict] = []
        for expected in baseline.get("services", []):
            port = expected.get("port")
            actual = current.get(port)
            if actual is None:
                findings.append({
                    "port": port, "ok": False,
                    "detail": f"no service listening on port {port} after restore",
                })
                continue
            drift = []
            if actual.get("cards") != expected.get("cards"):
                drift.append(
                    f"parallelism: was {expected.get('cards')} card(s), now {actual.get('cards')}"
                )
            if actual.get("image") != expected.get("image"):
                drift.append(f"image: was {expected.get('image')}, now {actual.get('image')}")
            if actual.get("served_model_name") != expected.get("served_model_name"):
                drift.append(
                    f"served model: was {expected.get('served_model_name')}, "
                    f"now {actual.get('served_model_name')}"
                )
            findings.append({
                "port": port,
                "ok": not drift,
                "detail": "; ".join(drift) or "matches capture",
            })
        return findings

    def environment(self, handle: DeploymentHandle) -> dict:
        """Image digest + engine version, read off the running container.

        One ssh round-trip against a run that lasts ~90 minutes. Everything is
        individually optional: an engine that will not report `--version`
        still yields a usable digest, and a total failure yields `{}` rather
        than costing the run.
        """
        snapshot: dict = {}
        try:
            inspected = self._ssh(
                handle.machine,
                self._q(
                    [
                        "docker", "inspect", "-f",
                        "{{index .RepoDigests 0}}\t{{.Image}}\t{{.Config.Image}}",
                        handle.container_name,
                    ]
                ),
            )
            if inspected.returncode == 0:
                digest, image_id, tag = (
                    inspected.stdout.strip().split("\t") + ["", "", ""]
                )[:3]
                # RepoDigests is empty for a locally-built image; the image ID
                # still pins the exact bits, which is what reproduction needs.
                snapshot["image_digest"] = digest
                snapshot["image_id"] = image_id
                snapshot["image_tag"] = tag

            versions = self._ssh(
                handle.machine,
                f"docker exec {shlex.quote(handle.container_name)} python3 -c "
                + shlex.quote(_VERSION_PROBE),
                timeout=60,
            )
            if versions.returncode == 0 and versions.stdout.strip():
                try:
                    snapshot.update(json.loads(versions.stdout.strip().splitlines()[-1]))
                except (json.JSONDecodeError, IndexError):
                    pass
        except (RuntimeError, subprocess.SubprocessError) as exc:
            # Provenance is worth having, never worth failing a run for.
            logger.warning(
                "environment snapshot for %s failed: %s", handle.container_name, exc
            )
        return {k: v for k, v in snapshot.items() if v}

    # -- helpers -------------------------------------------------------------

    def _check_port_free(self, machine, port: int) -> None:
        result = self._ssh(
            machine,
            f"ss -lntp 2>/dev/null | awk '$4 ~ /:{port}$/ {{print}}' | head -1",
        )
        listener = result.stdout.strip()
        if listener:
            raise RuntimeError(
                f"port {port} already in use on {machine.host}: {listener[:300]}"
            )
        # A free port is not necessarily a *reachable* one: on k8s nodes
        # kube-proxy hijacks the NodePort range (30000-32767) on the node IP,
        # so the service would bind fine yet be unreachable from anywhere but
        # localhost — a failure that otherwise only shows up as a 30-minute
        # readiness timeout.
        if 30000 <= port <= 32767:
            probe = self._ssh(
                machine,
                "command -v kube-proxy >/dev/null || pgrep -x kube-proxy >/dev/null && echo k8s",
            )
            if "k8s" in probe.stdout:
                raise RuntimeError(
                    f"port {port} is inside the k8s NodePort range (30000-32767) and "
                    f"{machine.host} runs kube-proxy, which hijacks that range on the "
                    "node IP — choose a port outside it (e.g. 28200)"
                )

    def _dump_remote_logs(self, machine, container_name: str) -> None:
        script = (
            f"if docker ps -aq --filter name=^{container_name}$ | grep -q .; then "
            f"mkdir -p {REMOTE_LOG_DIR} && "
            f"docker logs {container_name} > "
            f"{REMOTE_LOG_DIR}/{container_name}-$(date +%Y%m%d-%H%M%S).log 2>&1 || true; "
            f"ls -1t {REMOTE_LOG_DIR}/*.log 2>/dev/null | tail -n +{KEEP_LOG_DUMPS + 1} "
            f"| xargs -r rm -f || true; fi"
        )
        try:
            self._ssh(machine, script, timeout=120)
        except subprocess.TimeoutExpired:
            logger.warning("remote log dump timed out for %s on %s", container_name, machine.host)

    @staticmethod
    def _endpoint_ready(endpoint_url: str) -> bool:
        try:
            response = httpx.get(f"{endpoint_url}/v1/models", timeout=5)
            return response.status_code == 200
        except httpx.HTTPError:
            return False


# Classification moved to `failures.py` — it is substrate-neutral, and the
# orchestrator was importing this concrete driver just to reach it. Re-exported
# here so existing call sites and tests keep working.
__all__ = [
    "MODEL_MOUNT",
    "SshDockerDriver",
    "classify_exit",
    "classify_failure",
    "render_docker_command",
    "render_engine_command",
]
