"""The winner's exact, reproducible configuration — what crosses the seam.

A leaderboard row says "run 214 won". A rollout needs more than an id: the
precise engine arguments, the *pinned* image (a tag is mutable; the digest the
run actually executed is not), the served model name, and the exact server
command — the same argv the ssh and k8s drivers build, so what production
serves is byte-for-byte what was measured. That is this module's whole job:
turn (campaign, candidate, run, result) into that payload.

It also carries the *evidence*: the metric it won on, its value, its multiple
of the production baseline, and which dataset build measured it. A rollout is a
decision someone signs off on, and the number that justifies it should travel
with the config, not be re-derived from a dashboard later.
"""

from __future__ import annotations

from typing import Any

from app.control.engines import get_adapter
from app.control.launch.base import LaunchSpec, MachineInfo
from app.control.search.validation import cards_used
from app.db.models import Campaign, Candidate, Result, Run


def _pinned_image(campaign: Campaign, run: Run) -> dict[str, str]:
    """The image to deploy, pinned as tightly as the run recorded.

    The campaign names a tag; the run's env snapshot may hold the digest and
    image id read off the live container. Prefer the digest — a tag two weeks
    apart can mean two different builds, and a rollout must ship the bits that
    were measured, not whatever the tag points at on rollout day.
    """
    snapshot = run.env_snapshot or {}
    # A policy-served run may have run its contender on a different engine tag
    # than the campaign's (declared in its launch_spec, stamped into the
    # snapshot as image_tag). That tag is the engine that was measured, so it
    # outranks the campaign's — but a snapshot digest is only trusted when it
    # belongs to an ENGINE image: for a policy-served run the digest was read
    # off (or reported by) the serving side, and pinning the POLICY container's
    # digest would roll the search program out as the inference service.
    tag = snapshot.get("image_tag") or campaign.image
    out = {"tag": tag}
    if snapshot.get("image_digest"):
        out["digest"] = snapshot["image_digest"]
    if snapshot.get("image_id"):
        out["image_id"] = snapshot["image_id"]
    # The reference to actually deploy: digest if we have it, else the tag.
    out["ref"] = out.get("digest") or tag
    return out


def _command(campaign: Campaign, engine_args: dict[str, Any]) -> list[str]:
    """The exact server command these engine args produce, via the same adapter
    the drivers use — so "the promoted config" and "what ran" cannot diverge.

    Built from a minimal LaunchSpec: only the fields the engine command reads
    (model path, served name, port, args) matter here; placement is the
    cluster's problem at rollout, not part of the config being promoted.
    """
    spec = LaunchSpec(
        run_id=0,
        machine=MachineInfo(name="promotion", host="", gpu_count=0),
        engine=campaign.engine,
        image=campaign.image,
        model_path=campaign.model_path,
        served_model_name=campaign.served_model_name,
        engine_args=engine_args or {},
        port=campaign.service_port,
        env=campaign.extra_env or {},
        volumes=campaign.extra_volumes or {},
    )
    return get_adapter(campaign.engine).build_command(spec)


def build_promotion_config(
    campaign: Campaign,
    candidate: Candidate,
    run: Run,
    result: Result | None = None,
) -> dict[str, Any]:
    """The full payload handed to a PromotionTarget.

    Pure and side-effect free: given the four rows, it is the same config every
    time, so two operators promoting the same run get the same artifact.
    """
    engine_args = dict(candidate.config or {})
    evidence: dict[str, Any] = {}
    if result is not None:
        metrics = result.metrics or {}
        evidence = {
            "score": result.objective_value if result.objective_value is not None else result.score,
            "target_metric": (campaign.objective or {}).get("target_metric", ""),
            "feasible": bool(result.feasible),
            "breaches": list(result.breaches or []),
            # How this result knows it is comparable to the baseline/other
            # nights — the replayed build. From the flat replay metrics.
            "dataset_build_id": str(metrics.get("dataset_id", "") or ""),
        }
    return {
        "campaign_id": campaign.id,
        "run_id": run.id,
        "engine": campaign.engine,
        "served_model_name": campaign.served_model_name,
        "model_path": campaign.model_path,
        "image": _pinned_image(campaign, run),
        # The tuned knobs, in the same snake_case shape the search space speaks.
        "engine_args": engine_args,
        "cards": cards_used(engine_args),
        "port": campaign.service_port,
        "extra_env": dict(campaign.extra_env or {}),
        "extra_volumes": dict(campaign.extra_volumes or {}),
        # The reproducible server command — element 0 is the entrypoint.
        "command": _command(campaign, engine_args),
        # What actually ran, for the record that ships with the rollout.
        "env_snapshot": dict(run.env_snapshot or {}),
        "evidence": evidence,
    }
