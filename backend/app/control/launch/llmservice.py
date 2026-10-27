"""The LLMService driver: one run, one LLMService.

A run is launched as the object a production deploy is: an LLMService
(llm-operator) installing the engine chart, with the run's recipe as its
catalog layer. What the tuner measured is therefore what the chart renders,
and a winning recipe drops into the model catalog with nothing translated.

The operator installs the chart and reports the Helm outcome, and nothing
more: no readiness, no endpoint, no logs. So this driver keeps reading the
pods itself, the way the k8s driver it extends always has (state, failure
reasons, logs, the environment), selecting them by the run label the run
layer puts on every pod the chart makes.

    launch    compose catalog + site + run layers, apply the LLMService
    state     the operator's phase (Failed is a config the chart refused),
              then the pods (crashed, wedged, still coming up), then the front
              door: READY when the chart's Service has a ready endpoint, which
              is the leader under lws and the routers under pd
    endpoint  in-cluster DNS by default, `<service>.<ns>.svc:<port>`; a cluster
              set to "nodeport" gets the Service's node port on a node
    teardown  delete the LLMService; gone only when it AND its pods are gone,
              because an operator before 0.5.1 drops the object at uninstall
              while the pods are still terminating on their cards
"""

from __future__ import annotations

import json
import logging
from typing import Any

import yaml

from app.control.launch.base import DeploymentHandle, DeploymentState, LaunchSpec
from app.control.launch.k8s import RUN_LABEL, K8sDriver, _node_selector, _tolerations
from app.control.launch.k8s_api import LLMSVC_RESOURCE
from app.control.recipe import Recipe
from app.control.recipe.check import recipe_problems
from app.control.recipe.compose import (
    RunPlan,
    Site,
    llmservice,
    merge,
    service_name,
    service_port,
)
from app.control.recipe.legacy import from_fields

logger = logging.getLogger(__name__)

# The engine container's name in each chart's pod (`sglang`/`vllm`); the
# others are the hang-watcher sidecar and, under pd, the router.
_ENGINE_CONTAINERS = ("sglang", "vllm")


def recipe_of(spec: LaunchSpec) -> Recipe:
    """The run's recipe: its own when the campaign stores one, else built from
    today's campaign fields (recipe/legacy.py)."""
    if spec.recipe:
        return Recipe.model_validate(spec.recipe)
    return from_fields(
        engine=spec.engine,
        image=spec.image,
        served_model_name=spec.served_model_name,
        engine_args=spec.engine_args,
        extra_env=spec.env,
        extra_volumes=spec.volumes,
        nodes=spec.nnodes,
    )


class LLMServiceDriver(K8sDriver):
    name = "k8s"

    @property
    def _resource(self) -> str:
        return LLMSVC_RESOURCE

    def _run_selector(self, spec_or_handle) -> str:
        if getattr(spec_or_handle, "workload", False):
            return super()._run_selector(spec_or_handle)
        run = getattr(spec_or_handle, "run_id", None)
        if run is None:
            name = getattr(spec_or_handle, "container_name", "") or ""
            run = name.rsplit("-", 1)[-1] if name else ""
        return f"{RUN_LABEL}={run}"

    # -- composition ---------------------------------------------------------

    def site(self, spec: LaunchSpec, recipe: Recipe) -> Site:
        s = self.settings
        extra: dict[str, Any] = {}
        if s.k8s_shm_size_mb:
            extra["shm"] = {"enabled": True, "sizeLimit": f"{int(s.k8s_shm_size_mb)}Mi"}
        requests = {
            k: v
            for k, v in (("cpu", s.k8s_engine_cpu_request), ("memory", s.k8s_engine_memory_request))
            if v
        }
        limits = {
            k: v
            for k, v in (("cpu", s.k8s_engine_cpu_limit), ("memory", s.k8s_engine_memory_limit))
            if v
        }
        if requests or limits:
            extra["resources"] = {
                **({"requests": requests} if requests else {}),
                **({"limits": limits} if limits else {}),
            }
        if s.k8s_site_values.strip():
            try:
                site_values = json.loads(s.k8s_site_values)
            except ValueError as exc:
                raise RuntimeError(
                    f"cluster setup: the cluster's site values are not JSON ({exc})"
                ) from exc
            if not isinstance(site_values, dict):
                raise RuntimeError("cluster setup: the cluster's site values must be a JSON object")
            extra = merge(extra, site_values)
        gpus = max(1, recipe.gpus)
        return Site(
            namespace=s.k8s_namespace,
            chart_repo=s.k8s_chart_repo,
            chart_credentials_secret=s.k8s_chart_credentials_secret,
            model_local_path=spec.model_path,
            image_mirror=s.k8s_image_mirror,
            node_selector=_node_selector(spec, s),
            tolerations=_tolerations(gpus, s),
            extra_values=extra,
            endpoint=s.k8s_endpoint or "dns",
        )

    def manifest(self, spec: LaunchSpec) -> tuple[Recipe, dict[str, Any]]:
        if self.settings.k8s_model_pvc:
            raise RuntimeError(
                "cluster setup: this cluster keeps weights on a PVC, and the engine charts "
                "mount them from the node (model.localPath); use the deployment workload kind "
                "here until the charts can mount a PVC"
            )
        recipe = recipe_of(spec)
        blocking = recipe_problems(recipe)
        if blocking:
            raise RuntimeError(
                "the chart refuses this config: " + "; ".join(p["message"] for p in blocking)
            )
        run = RunPlan(
            run_id=spec.run_id,
            release=spec.container_name,
            allowed_nodes=list(spec.allowed_nodes),
        )
        return recipe, llmservice(recipe, self.site(spec, recipe), run)

    # -- DeploymentDriver ----------------------------------------------------

    def launch(self, spec: LaunchSpec) -> tuple[DeploymentHandle, str]:
        recipe, obj = self.manifest(spec)
        self.api.apply(obj)
        handle = DeploymentHandle(
            driver=self.name,
            container_name=spec.container_name,
            machine=spec.machine,
            endpoint_url=self._endpoint(spec, recipe),
        )
        return handle, "kubectl apply -f - <<'EOF'\n" + yaml.safe_dump(
            obj, sort_keys=False, width=1000
        ) + "EOF"

    def attach(self, spec: LaunchSpec) -> DeploymentHandle | None:
        if self.api.get(self._resource, spec.container_name) is None:
            return None
        return DeploymentHandle(
            driver=self.name,
            container_name=spec.container_name,
            machine=spec.machine,
            endpoint_url=self._endpoint(spec, recipe_of(spec)),
        )

    def state(self, handle: DeploymentHandle) -> DeploymentState:
        obj = self.api.get(self._resource, handle.container_name)
        if obj is None:
            return DeploymentState.GONE
        if self._operator_failure(obj) is not None:
            return DeploymentState.CRASHED
        pods = self._pod_state(self._run_selector(handle))
        if pods == DeploymentState.CRASHED:
            return DeploymentState.CRASHED
        reason = self.failure_reason(handle)
        if reason is not None and reason[0] not in _transient():
            return DeploymentState.CRASHED
        ready = self._front_door_ready(obj, handle)
        if ready is None:
            # Endpoints unreadable: the pods' own readiness is the next best
            # signal. Not exact under lws, whose workers have no probe.
            return pods
        return DeploymentState.READY if ready else DeploymentState.STARTING

    def failure_reason(self, handle: DeploymentHandle) -> tuple[str, str] | None:
        try:
            obj = self.api.get(self._resource, handle.container_name)
        except Exception:
            obj = None
        failure = self._operator_failure(obj) if obj else None
        if failure is not None:
            return failure
        return super().failure_reason(handle)

    def launch_nodes(self, spec: LaunchSpec) -> tuple[DeploymentHandle, list[str]]:
        """One object, however many pods: the chart makes the gang (lws) or the
        role pair (pd) itself."""
        handle, command = self.launch(spec)
        return handle, [command]

    def teardown(self, handle: DeploymentHandle) -> None:
        if handle.workload:
            super().teardown(handle)
            return
        self.api.delete(self._resource, handle.container_name)

    def is_gone(self, handle: DeploymentHandle) -> bool:
        if handle.workload:
            return super().is_gone(handle)
        if self.api.get(self._resource, handle.container_name) is not None:
            return False
        try:
            return not self.api.list("pods", self._run_selector(handle))
        except Exception:
            return False  # unproven: the cards stay held

    def environment(self, handle: DeploymentHandle) -> dict[str, Any]:
        snapshot: dict[str, Any] = {}
        try:
            pods = self.api.list("pods", self._run_selector(handle))
            for pod in pods:
                for container in (pod.get("spec") or {}).get("containers") or []:
                    if container.get("name") in _ENGINE_CONTAINERS and container.get("image"):
                        snapshot.setdefault("image_tag", container["image"])
                for status in (pod.get("status") or {}).get("containerStatuses") or []:
                    image_id = str(status.get("imageID") or "")
                    if status.get("name") in _ENGINE_CONTAINERS and "@sha256:" in image_id:
                        snapshot.setdefault("image_digest", image_id.split("://", 1)[-1])
                node = (pod.get("spec") or {}).get("nodeName")
                if node and "k8s_node" not in snapshot:
                    snapshot["k8s_node"] = node
                    card_type = self._node_card_type(node)
                    if card_type:
                        snapshot["card_type"] = card_type
            obj = self.api.get(self._resource, handle.container_name) or {}
            chart = (obj.get("status") or {}).get("chart") or {}
            if chart.get("version"):
                snapshot["chart"] = f"{chart.get('name')}-{chart['version']}"
        except Exception as exc:
            logger.warning("llmservice environment for %s failed: %s", handle.container_name, exc)
        return {k: v for k, v in snapshot.items() if v}

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _operator_failure(obj: dict[str, Any]) -> tuple[str, str] | None:
        """The operator's own verdict, when it is final.

        `Failed` covers three different things. The chart refusing these values
        (a render guard: Helm's "execution error") is a verdict on the config.
        A repository it may not use or cannot resolve, a release it does not
        own, is the cluster's setup, and every config would meet it. Any other
        apply error the operator retries with backoff, so it is not final."""
        status = obj.get("status") or {}
        if status.get("phase") != "Failed":
            return None
        reason = ""
        for condition in status.get("conditions") or []:
            if condition.get("type") == "Applied" and condition.get("status") == "False":
                reason = condition.get("reason") or ""
        message = str(status.get("message") or reason or "the operator could not apply the chart")
        if "execution error" in message or reason == "Failed":
            return ("bad_config", f"the chart refuses this config: {message}"[:2000])
        if reason in ("ChartRepoNotAllowed", "ChartResolveFailed", "Drift", "PendingRelease"):
            return ("cluster_setup", f"cluster setup: {reason}: {message}"[:2000])
        return None

    def _front_door_ready(self, obj: dict[str, Any], handle: DeploymentHandle) -> bool | None:
        name = service_name(self._recipe_from_obj(obj), handle.container_name)
        try:
            endpoints = self.api.get("endpoints", name)
        except Exception:
            return None
        if endpoints is None:
            return False
        return any(subset.get("addresses") for subset in endpoints.get("subsets") or [])

    @staticmethod
    def _recipe_from_obj(obj: dict[str, Any]) -> Recipe:
        layers = (obj.get("spec") or {}).get("layers") or []
        catalog = next((layer for layer in layers if layer.get("name") == "catalog"), {})
        return Recipe(values=catalog.get("values") or {})

    def _endpoint(self, spec: LaunchSpec, recipe: Recipe) -> str:
        name = service_name(recipe, spec.container_name)
        port = service_port(recipe)
        if (self.settings.k8s_endpoint or "dns") != "nodeport":
            return f"http://{name}.{self.settings.k8s_namespace}.svc:{port}"
        svc = self.api.get("service", name)
        ports = ((svc or {}).get("spec") or {}).get("ports") or []
        node_port = ports[0].get("nodePort") if ports else None
        host = self.settings.k8s_node_host or self._first_node_ip()
        if host and node_port:
            return f"http://{host}:{node_port}"
        # The operator creates the Service after the apply returns; attach()
        # re-resolves once the run is ready.
        return ""


def _transient() -> frozenset[str]:
    from app.control.launch.failures import TRANSIENT_PLACEMENT_FAILURES

    return frozenset(TRANSIENT_PLACEMENT_FAILURES)
