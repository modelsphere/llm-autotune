"""The cluster transport, isolated behind a narrow interface.

The k8s driver's *logic* — render a LaunchSpec into a workload, map its status
onto a DeploymentState, find it again by name, delete it idempotently — is
worth testing now, before a cluster exists. What that logic cannot have baked
in is *how the API is actually reached*: shelling out to kubectl, an in-cluster
Python client, or a REST call are all plausible and none is decided. So the
transport is this small ABC, and the driver is written against it.

Two shipping implementations:

- `KubectlApi` shells out to the `kubectl` binary, inheriting the worker's
  kubeconfig / service-account exactly as the ssh driver inherits ~/.ssh. It is
  the pragmatic first substrate: it works wherever a kubeconfig does, needs no
  Python client dependency, and its calls read like the commands an operator
  would run by hand.
- `UnavailableK8sApi` is the default until the migration lands. Every call
  raises `K8sUnavailable` with a message that says what to configure, so a
  half-configured platform fails loudly and specifically instead of with a
  cryptic connection error deep in a launch.

Tests supply their own in-memory fake — the whole reason the seam is this
shape.

Resources are addressed by a `resource` string (`get`/`delete`/`list`) so the
same transport reaches both the custom resource (`inferenceservices.<group>`)
and the pods the operator creates under it (`pods`); the driver knows the
names, the transport just runs the call.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import subprocess
import tempfile
from abc import ABC, abstractmethod
from typing import Any

import yaml

from app.control.launch.clusters import K8sClusterSettings, as_cluster

logger = logging.getLogger(__name__)


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


class K8sError(RuntimeError):
    """A cluster call failed. Raised as RuntimeError so the supervisor
    classifies it as an infrastructure failure (retryable) rather than letting
    it escape as an unexpected supervisor crash — same contract as the ssh
    driver's timeouts."""


class K8sUnavailable(K8sError):
    """The k8s substrate was used but never configured. Distinct from a
    transient cluster error: this one never succeeds on retry."""


class K8sApi(ABC):
    """Everything the k8s driver needs from the cluster, and nothing more."""

    @abstractmethod
    def apply(self, manifest: dict | list[dict]) -> None:
        """Create-or-update a resource, or a list of them, from manifest(s)
        (server-side apply). Idempotent: relaunching the same run re-applies the
        same objects. A list is applied atomically enough for our purpose —
        a workload and its Service go up together."""

    @abstractmethod
    def get(self, resource: str, name: str) -> dict | None:
        """The named object as parsed JSON, or None if it does not exist."""

    @abstractmethod
    def delete(self, resource: str, name: str) -> None:
        """Delete the named object. Idempotent — a missing object is success,
        because teardown must succeed on a half-dead deployment."""

    @abstractmethod
    def list(self, resource: str, label_selector: str = "") -> list[dict]:
        """Objects of a kind, optionally narrowed by label selector."""

    @abstractmethod
    def logs(self, label_selector: str, tail: int = 200) -> str:
        """Recent logs from the pods matching a selector (the operator labels a
        CR's pods with its name), concatenated. Best-effort: returns '' rather
        than raising, because a log we cannot read must never fail a run."""


class UnavailableK8sApi(K8sApi):
    """The default until the cluster migration lands: every call explains what
    is missing instead of failing obscurely."""

    _MSG = (
        "the k8s launch driver is selected but not configured — set "
        "AUTOTUNE_K8S_API_MODE=kubectl (and the k8s_* cluster settings) once the "
        "GPU cluster migration is live"
    )

    def apply(self, manifest: dict | list[dict]) -> None:
        raise K8sUnavailable(self._MSG)

    def get(self, resource: str, name: str) -> dict | None:
        raise K8sUnavailable(self._MSG)

    def delete(self, resource: str, name: str) -> None:
        raise K8sUnavailable(self._MSG)

    def list(self, resource: str, label_selector: str = "") -> list[dict]:
        raise K8sUnavailable(self._MSG)

    def logs(self, label_selector: str, tail: int = 200) -> str:
        raise K8sUnavailable(self._MSG)


class KubectlApi(K8sApi):
    """Reaches the cluster by shelling out to `kubectl`.

    Mirrors the ssh driver's use of the system `ssh` binary: no client library,
    the ambient kubeconfig / in-cluster service account supplies credentials,
    and `--namespace`/`--context` come from settings so one worker can be
    pointed at one cluster slice. Every call is bounded by a timeout and
    converts a non-zero exit into `K8sError`, so a wedged cluster call is a
    classified failure rather than a hang.
    """

    def __init__(self, cluster: K8sClusterSettings | Any | None = None):
        self.cluster = as_cluster(cluster)
        self._kc_path: str | None = None

    def _materialized_kubeconfig(self) -> str:
        """kubectl needs a FILE, but a cluster credential lives encrypted in the
        DB. Write it once per transport, mode 0600, and remove it at exit."""
        if self._kc_path is None:
            fd, path = tempfile.mkstemp(prefix="autotune-kubeconfig-", suffix=".yaml")
            with os.fdopen(fd, "w") as handle:
                handle.write(self.cluster.kubeconfig_content)
            os.chmod(path, 0o600)
            atexit.register(_unlink_quietly, path)
            self._kc_path = path
        return self._kc_path

    def _kubeconfig_args(self) -> list[str]:
        if self.cluster.kubeconfig_content:
            return ["--kubeconfig", self._materialized_kubeconfig()]
        if self.cluster.k8s_kubeconfig:
            return ["--kubeconfig", self.cluster.k8s_kubeconfig]
        return []

    def _base(self) -> list[str]:
        cmd = [self.cluster.k8s_kubectl_bin, "--namespace", self.cluster.k8s_namespace]
        cmd += self._kubeconfig_args()
        if self.cluster.k8s_context:
            cmd += ["--context", self.cluster.k8s_context]
        return cmd

    def _run(self, args: list[str], *, stdin: str | None = None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                self._base() + args,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=self.cluster.k8s_call_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise K8sError(
                f"kubectl {' '.join(args[:2])} timed out after "
                f"{self.cluster.k8s_call_timeout}s"
            ) from exc
        except FileNotFoundError as exc:
            raise K8sUnavailable(
                f"kubectl binary '{self.cluster.k8s_kubectl_bin}' not found on the worker"
            ) from exc

    def apply(self, manifest: dict | list[dict]) -> None:
        # A list of objects is wrapped in a v1 List, which `kubectl apply -f -`
        # unpacks — one round-trip for the workload and its Service.
        doc = (
            {"apiVersion": "v1", "kind": "List", "items": manifest}
            if isinstance(manifest, list)
            else manifest
        )
        result = self._run(["apply", "-f", "-"], stdin=json.dumps(doc))
        if result.returncode != 0:
            raise K8sError(f"kubectl apply failed: {result.stderr.strip()[:2000]}")

    def get(self, resource: str, name: str) -> dict | None:
        result = self._run(["get", resource, name, "-o", "json"])
        if result.returncode != 0:
            # `kubectl get` says "NotFound" on stderr for a missing object; that
            # is None (the object is gone), not an error.
            if "NotFound" in result.stderr or "not found" in result.stderr.lower():
                return None
            raise K8sError(f"kubectl get {resource}/{name} failed: {result.stderr.strip()[:500]}")
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise K8sError(f"kubectl get {resource}/{name} returned non-JSON") from exc

    def delete(self, resource: str, name: str) -> None:
        result = self._run(["delete", resource, name, "--ignore-not-found", "--wait=false"])
        if result.returncode != 0 and "NotFound" not in result.stderr:
            raise K8sError(
                f"kubectl delete {resource}/{name} failed: {result.stderr.strip()[:500]}"
            )

    def list(self, resource: str, label_selector: str = "") -> list[dict]:
        args = ["get", resource, "-o", "json"]
        if label_selector:
            args += ["-l", label_selector]
        result = self._run(args)
        if result.returncode != 0:
            raise K8sError(f"kubectl get {resource} failed: {result.stderr.strip()[:500]}")
        try:
            return json.loads(result.stdout).get("items", [])
        except (json.JSONDecodeError, AttributeError):
            return []

    def logs(self, label_selector: str, tail: int = 200) -> str:
        # --all-containers so the engine's log is captured even when the pod
        # runs a sidecar; --prefix so multi-pod output stays attributable.
        result = self._run(
            ["logs", "-l", label_selector, "--tail", str(tail), "--all-containers", "--prefix"]
        )
        if result.returncode != 0:
            logger.warning("kubectl logs -l %s failed: %s", label_selector, result.stderr[:300])
            return ""
        return (result.stdout + "\n" + result.stderr).strip()


class ClientApi(K8sApi):
    """Reaches the cluster with the official Kubernetes Python client.

    The better transport for a containerized backend: with no kubeconfig it uses
    the pod's own ServiceAccount (`load_incluster_config`), so credentials are a
    scoped, auto-rotated token rather than a mounted admin file. Falls back to a
    kubeconfig (`k8s_kubeconfig` / `k8s_context`) when not running as a pod.

    The `kubernetes` import and the config load are BOTH lazy, so importing this
    module (and constructing the object) never needs the dependency or a cluster
    — only the first real call does. Typed responses are run through
    `sanitize_for_serialization`, so every method returns the same camelCase JSON
    dicts `kubectl -o json` produces and the driver already reads.
    """

    def __init__(self, cluster: K8sClusterSettings | Any | None = None):
        self.cluster = as_cluster(cluster)
        self._core = None  # set on first use by _ensure()

    @property
    def _ns(self) -> str:
        return self.cluster.k8s_namespace

    def _cr(self) -> tuple[str, str, str]:
        """(group, version, plural) for the custom resource, from the cluster."""
        s = self.cluster
        return (s.k8s_cr_group, s.k8s_cr_version, s.k8s_cr_plural)

    def _resource_for_kind(self, kind: str) -> str:
        """The get()/delete() resource string for a manifest kind — so apply()'s
        conflict path can re-read what it just tried to create."""
        if kind == "Deployment":
            return "deployment"
        if kind == "Service":
            return "service"
        if kind == "Job":
            return "job"
        return f"{self.cluster.k8s_cr_plural}.{self.cluster.k8s_cr_group}"

    def _ensure(self) -> None:
        if self._core is not None:
            return
        try:
            from kubernetes import client, config
            from kubernetes.client.rest import ApiException
        except ImportError as exc:  # pragma: no cover - dep is declared
            raise K8sUnavailable(
                "k8s_api_mode=client needs the 'kubernetes' package installed"
            ) from exc
        s = self.cluster
        # A Configuration per client, never the module-global default. The old
        # pattern (`load_kube_config()` then `ApiClient()`) writes the PROCESS
        # default, so loading a second cluster's kubeconfig silently re-pointed
        # the first client at it — the reason one worker could only serve one
        # cluster. `client_configuration=` keeps every cluster's client isolated.
        cfg = client.Configuration()
        if s.k8s_in_cluster:
            # Only when the backend itself is a pod INSIDE the GPU cluster.
            config.load_incluster_config(client_configuration=cfg)
        elif s.kubeconfig_content:
            # The normal path now: the scoped kubeconfig stored (encrypted) on
            # the cluster row, loaded straight from memory — never written to
            # disk for the client transport.
            config.load_kube_config_from_dict(
                yaml.safe_load(s.kubeconfig_content),
                context=s.k8s_context or None,
                client_configuration=cfg,
            )
        elif s.k8s_kubeconfig:
            # An env-named file, the pre-clusters default.
            config.load_kube_config(
                config_file=s.k8s_kubeconfig,
                context=s.k8s_context or None,
                client_configuration=cfg,
            )
        else:
            # Ambient kubeconfig (KUBECONFIG / ~/.kube/config) — dev convenience.
            config.load_kube_config(context=s.k8s_context or None, client_configuration=cfg)
        self._ApiException = ApiException
        self._api_client = client.ApiClient(configuration=cfg)
        self._core = client.CoreV1Api(self._api_client)
        self._apps = client.AppsV1Api(self._api_client)
        self._batch = client.BatchV1Api(self._api_client)
        self._custom = client.CustomObjectsApi(self._api_client)

    def _sanitize(self, obj) -> dict:
        """Typed model -> the camelCase JSON dict the driver reads."""
        return self._api_client.sanitize_for_serialization(obj)

    def _writers(self, kind: str):
        """(create, replace) callables for a manifest kind."""
        if kind == "Deployment":
            return (
                lambda item: self._apps.create_namespaced_deployment(self._ns, item),
                lambda name, item: self._apps.replace_namespaced_deployment(name, self._ns, item),
            )
        if kind == "Service":
            return (
                lambda item: self._core.create_namespaced_service(self._ns, item),
                lambda name, item: self._core.replace_namespaced_service(name, self._ns, item),
            )
        if kind == "Job":
            return (
                lambda item: self._batch.create_namespaced_job(self._ns, item),
                lambda name, item: self._batch.replace_namespaced_job(name, self._ns, item),
            )
        g, v, plural = self._cr()
        return (
            lambda item: self._custom.create_namespaced_custom_object(g, v, self._ns, plural, item),
            lambda name, item: self._custom.replace_namespaced_custom_object(
                g, v, self._ns, plural, name, item
            ),
        )

    def apply(self, manifest: dict | list[dict]) -> None:
        self._ensure()
        items = manifest if isinstance(manifest, list) else [manifest]
        for item in items:
            self._apply_one(item)

    def _apply_one(self, item: dict) -> None:
        kind = item.get("kind", "")
        name = (item.get("metadata") or {}).get("name")
        create, replace = self._writers(kind)
        try:
            create(item)
        except self._ApiException as exc:
            # Already there (idempotent relaunch): carry the live resourceVersion
            # and replace, so apply is create-or-update like `kubectl apply`.
            if exc.status != 409:
                raise K8sError(f"client apply {kind}/{name} failed: {exc.reason}") from exc
            current = self.get(self._resource_for_kind(kind), name)
            if current is not None:
                rv = (current.get("metadata") or {}).get("resourceVersion")
                item.setdefault("metadata", {})["resourceVersion"] = rv
            replace(name, item)

    def get(self, resource: str, name: str) -> dict | None:
        self._ensure()
        try:
            if resource == "deployment":
                return self._sanitize(self._apps.read_namespaced_deployment(name, self._ns))
            if resource == "service":
                return self._sanitize(self._core.read_namespaced_service(name, self._ns))
            if resource == "job":
                return self._sanitize(self._batch.read_namespaced_job(name, self._ns))
            g, v, plural = self._cr()
            return self._custom.get_namespaced_custom_object(g, v, self._ns, plural, name)
        except self._ApiException as exc:
            if exc.status == 404:
                return None
            raise K8sError(f"client get {resource}/{name} failed: {exc.reason}") from exc

    def delete(self, resource: str, name: str) -> None:
        self._ensure()
        try:
            if resource == "deployment":
                self._apps.delete_namespaced_deployment(name, self._ns)
            elif resource == "service":
                self._core.delete_namespaced_service(name, self._ns)
            elif resource == "job":
                # Background propagation is not optional: deleting a Job without
                # it orphans the pod, which keeps running — and keeps holding
                # whatever it holds — while `get job` reports it gone.
                self._batch.delete_namespaced_job(
                    name, self._ns, propagation_policy="Background"
                )
            else:
                g, v, plural = self._cr()
                self._custom.delete_namespaced_custom_object(g, v, self._ns, plural, name)
        except self._ApiException as exc:
            if exc.status == 404:
                return
            raise K8sError(f"client delete {resource}/{name} failed: {exc.reason}") from exc

    def list(self, resource: str, label_selector: str = "") -> list[dict]:
        self._ensure()
        kwargs = {"label_selector": label_selector} if label_selector else {}
        try:
            if resource == "pods":
                resp = self._core.list_namespaced_pod(self._ns, **kwargs)
            elif resource == "events":
                resp = self._core.list_namespaced_event(self._ns, **kwargs)
            elif resource == "nodes":
                resp = self._core.list_node(**kwargs)
            elif "." in resource:
                # A custom resource, addressed as `<plural>.<group>` — how the
                # probe asks whether the operator's CRD exists on this cluster.
                plural, _, group = resource.partition(".")
                return list(
                    self._custom.list_namespaced_custom_object(
                        group, self.cluster.k8s_cr_version, self._ns, plural
                    ).get("items")
                    or []
                )
            else:
                raise K8sError(f"client list of '{resource}' is not supported")
        except self._ApiException as exc:
            raise K8sError(f"client list {resource} failed: {exc.reason}") from exc
        return [self._sanitize(item) for item in resp.items]

    def logs(self, label_selector: str, tail: int = 200) -> str:
        self._ensure()
        try:
            pods = self._core.list_namespaced_pod(self._ns, label_selector=label_selector)
        except self._ApiException as exc:
            logger.warning("client list pods -l %s failed: %s", label_selector, exc.reason)
            return ""
        chunks = []
        for pod in pods.items:
            pod_name = pod.metadata.name
            try:
                text = self._core.read_namespaced_pod_log(pod_name, self._ns, tail_lines=tail)
            except self._ApiException:
                continue  # best-effort: a log we cannot read must not fail a run
            chunks.append(f"[{pod_name}]\n{text}")
        return "\n".join(chunks).strip()


# One transport per DB-bound cluster, so a worker serving several clusters does
# not rebuild a k8s client (and re-run credential loading) on every call. The
# default cluster is deliberately NOT cached: it is constructed from whatever
# Settings the caller holds, and construction is lazy, so caching it would let
# two different callers' settings collide.
_transports: dict[str, K8sApi] = {}


def get_k8s_api(cluster: K8sClusterSettings | Any | None = None) -> K8sApi:
    """The transport for one cluster. `unavailable` (the default) is the safe
    choice until a cluster is configured; `cluster` may be a DB
    `K8sClusterSettings`, the global `Settings`, or None (the default cluster)."""
    cfg = as_cluster(cluster)
    mode = cfg.k8s_api_mode
    if mode == "unavailable":
        return UnavailableK8sApi()
    if mode not in ("client", "kubectl"):
        raise K8sUnavailable(
            f"unknown k8s_api_mode '{mode}' (available: client, kubectl, unavailable)"
        )
    if cfg.is_default:
        return ClientApi(cfg) if mode == "client" else KubectlApi(cfg)
    key = f"{mode}:{cfg.cache_key}"
    hit = _transports.get(key)
    if hit is None:
        hit = ClientApi(cfg) if mode == "client" else KubectlApi(cfg)
        _transports[key] = hit
    return hit


def forget_transport(cluster_id: int | None = None) -> None:
    """Drop cached transports after a cluster edit, so the next call reloads the
    credential. `None` clears every DB-bound cluster's transport."""
    if cluster_id is None:
        _transports.clear()
        return
    prefix = f"cluster:{cluster_id}:"
    for key in [k for k in _transports if prefix in k]:
        _transports.pop(key, None)
