"""A report's frozen comparison, fit for publishing outside the platform.

A saved report freezes the whole comparison it was written from, and its HTML
file embeds it: run and campaign ids and names, links into this platform's
API, the machine and GPU indices a run used, host paths, the LLMBench
submission. Inside the platform that is provenance; in a public model catalog
it describes one site. This keeps what the report draws (the measurements,
deltas, settings, serving command and image) and drops or neutralizes the
rest, so the report renders the same and says nothing about where it ran.
"""

from __future__ import annotations

import copy
from typing import Any

from app.control.catalog_export.portability import classify_env

# Where the platform mounts a run's weights in its container.
MODEL_MOUNT = "/model"

_DROPPED_KEYS = frozenset({"links", "log"})
_BLANKED_KEYS = frozenset(
    {
        "url",
        "submission_id",
        "submission_url",
        "web_url",
        "config_hash_frozen",
        "config_hash_recorded",
        "dataset_build_id",
        "docker_command",
        "campaign_name",
        "error",
    }
)
_NULLED_KEYS = frozenset({"campaign_id", "candidate_id", "benchmark_id", "group"})


def publishable(comparison: dict[str, Any]) -> dict[str, Any]:
    doc = copy.deepcopy(comparison or {})
    runs = [doc.get("baseline") or {}, *(doc.get("attempts") or [])]
    ids = {r.get("run_id"): i + 1 for i, r in enumerate(runs) if r.get("run_id") is not None}
    labels = {
        r.get("run_id"): "Baseline"
        if i == 0
        else ("Optimized" if len(runs) == 2 else f"Optimized {i}")
        for i, r in enumerate(runs)
    }
    # Host paths a run used, and what the container calls them.
    paths: dict[str, str] = {}
    machines: set[str] = set()
    for r in runs:
        cfg = ((r.get("launch") or {}).get("config")) or {}
        if cfg.get("model_path"):
            paths[str(cfg["model_path"]).rstrip("/")] = MODEL_MOUNT
        for host, spec in (cfg.get("extra_volumes") or {}).items():
            paths[str(host).rstrip("/")] = str(spec).split(":", 1)[0]
        name = (((r.get("environment") or {}).get("machine")) or {}).get("name")
        if name:
            machines.add(str(name))
    for r in runs:
        _scrub_run(r, labels)
    platform = ((doc.get("benchmark") or {}).get("platform")) or {}
    if platform:
        platform["extensions"] = {}

    def text(value: str) -> str:
        for host in sorted(paths, key=len, reverse=True):
            if host:
                value = value.replace(host, paths[host])
        for name in machines:
            value = value.replace(name, "")
        return value

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            out = {}
            # Anything naming a run (a series line, the best run) gets its
            # public name, not the platform's "<campaign> · run <id>".
            named = node.get("run_id") in labels and "label" in node
            for k, v in node.items():
                if named and k == "label":
                    out[k] = labels[node["run_id"]]
                    continue
                if k in _DROPPED_KEYS:
                    continue
                if k in _BLANKED_KEYS:
                    out[k] = "" if isinstance(v, str) else None
                elif k in _NULLED_KEYS:
                    out[k] = None
                elif k == "run_id" and v in ids:
                    out[k] = ids[v]
                else:
                    out[k] = walk(v)
            return out
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, str):
            return text(node)
        return node

    return walk(doc)


def _scrub_run(run: dict[str, Any], labels: dict[Any, str]) -> None:
    run["label"] = labels.get(run.get("run_id"), "Optimized")
    launch = run.get("launch") or {}
    launch["origin"] = {"source": "campaign"}
    cfg = launch.get("config") or {}
    cfg["extra_volumes"] = {}
    cfg["extra_env"] = {
        k: v
        for k, v in (cfg.get("extra_env") or {}).items()
        if classify_env(k, str(v), [MODEL_MOUNT])[0] in ("recipe", "unknown")
    }
    env = run.get("environment") or {}
    if isinstance(env.get("machine"), dict):
        env["machine"] = {**env["machine"], "name": "", "driver": ""}
    env["gpu_indices"] = []
    results = run.get("results") or {}
    if isinstance(results.get("failure"), dict):
        results["failure"] = {**results["failure"], "log": "", "error": ""}
    for key in ("diff_vs_baseline", "diff_vs_previous"):
        diff = run.get(key) or {}
        env_diff = diff.get("extra_env") or {}
        for part in ("added", "removed", "changed"):
            names = env_diff.get(part)
            if isinstance(names, dict):
                env_diff[part] = {
                    k: v
                    for k, v in names.items()
                    if classify_env(k, "", [MODEL_MOUNT])[0] in ("recipe", "unknown")
                }
