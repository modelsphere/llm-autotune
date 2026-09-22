"""LaunchConfig — the one shape every engine configuration converts through.

The platform already carried this shape four times under four names: a
campaign plus its candidate config, the driver's LaunchSpec, a policy
contender's launch_spec, and the payload a pasted `docker run` line compiles
into. They agreed on the fields and disagreed on nothing, so naming it costs
little and buys a single conversion point: the campaign anchor, a baseline, a
winner and the deploy target's file all read and write THIS, never each
other.

Two conventions it fixes:
- `engine_args` is the canonical snake_case vocabulary of the flag catalog,
  aliases folded — so two configs compare knob by knob without spelling
  noise. `cards` is derived from it and never stored separately.
- Substrate details are not config. Where the engine listens, what it is
  called on the wire and where the weights are mounted inside the container
  are set per substrate (the ssh driver, the k8s driver, the Helm chart) and
  compare as "same" across substrates by definition. `service_port` and
  `served_model_name` are carried because launches need them, but a
  difference in them is never a difference in the configuration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from app.control.engines.flags import normalize_args
from app.control.search.validation import cards_used

if TYPE_CHECKING:  # pragma: no cover
    from app.db.models import Baseline, Campaign


# Fields whose difference is substrate, not configuration.
SUBSTRATE_FIELDS: frozenset[str] = frozenset({"service_port", "served_model_name"})
# Fields a deploy file or a hand-entered baseline may spell differently from
# the platform for reasons that are not performance — compared under an
# ownership policy, never silently.
IDENTITY_FIELDS: tuple[str, ...] = ("image", "model_path", "extra_env", "extra_volumes")


class LaunchConfig(BaseModel):
    engine: str = "sglang"
    image: str = ""
    model_path: str = ""
    served_model_name: str = ""
    service_port: int = 0
    engine_args: dict[str, Any] = Field(default_factory=dict)
    extra_env: dict[str, str] = Field(default_factory=dict)
    extra_volumes: dict[str, str] = Field(default_factory=dict)
    gpu_type: str = ""

    @property
    def cards(self) -> int:
        return cards_used(self.engine_args or {})

    def normalized(self) -> LaunchConfig:
        """A copy with engine_args in the canonical vocabulary."""
        args, _ = normalize_args(self.engine, dict(self.engine_args or {}))
        return self.model_copy(update={"engine_args": args})

    # -- conversions ---------------------------------------------------------

    @classmethod
    def from_campaign(
        cls,
        campaign: Campaign,
        engine_args: dict[str, Any] | None = None,
        gpu_type: str = "",
    ) -> LaunchConfig:
        """A campaign with the given knobs — a candidate's, or the search
        space's base when none are given."""
        if engine_args is None:
            space = getattr(campaign, "search_space", None) or {}
            engine_args = dict(space.get("base") or {})
        return cls(
            engine=campaign.engine,
            image=campaign.image or "",
            model_path=campaign.model_path or "",
            served_model_name=campaign.served_model_name or "",
            service_port=int(campaign.service_port or 0),
            engine_args=dict(engine_args or {}),
            extra_env=dict(campaign.extra_env or {}),
            extra_volumes=dict(campaign.extra_volumes or {}),
            gpu_type=gpu_type,
        ).normalized()

    @classmethod
    def from_baseline(cls, baseline: Baseline) -> LaunchConfig:
        return cls(
            engine=baseline.engine or "sglang",
            image=baseline.image or "",
            model_path=baseline.model_path or "",
            served_model_name=baseline.served_model_name or "",
            service_port=int(baseline.service_port or 0),
            engine_args=dict(baseline.engine_args or {}),
            extra_env=dict(baseline.extra_env or {}),
            extra_volumes=dict(baseline.extra_volumes or {}),
            gpu_type=baseline.card_type or "",
        ).normalized()

    @classmethod
    def from_promotion_config(cls, config: dict[str, Any], gpu_type: str = "") -> LaunchConfig:
        """The winner payload build_promotion_config produces."""
        image = config.get("image") or {}
        return cls(
            engine=config.get("engine") or "sglang",
            # The TAG the winner ran, not the digest: a deploy file names tags,
            # and the digest travels separately as evidence.
            image=str(image.get("tag") or image.get("ref") or ""),
            model_path=config.get("model_path") or "",
            served_model_name=config.get("served_model_name") or "",
            service_port=int(config.get("port") or 0),
            engine_args=dict(config.get("engine_args") or {}),
            extra_env=dict(config.get("extra_env") or {}),
            extra_volumes=dict(config.get("extra_volumes") or {}),
            gpu_type=gpu_type or str((config.get("env_snapshot") or {}).get("card_type") or ""),
        ).normalized()

    @classmethod
    def from_parsed(cls, parsed: dict[str, Any], engine: str = "sglang") -> LaunchConfig:
        """From parse_launch_command's dict, or a deploy-format parse result."""
        return cls(
            engine=parsed.get("engine") or engine,
            image=parsed.get("image") or "",
            model_path=parsed.get("model_path") or "",
            served_model_name=parsed.get("served_model_name") or "",
            service_port=int(parsed.get("service_port") or 0),
            engine_args=dict(parsed.get("engine_args") or {}),
            extra_env=dict(parsed.get("extra_env") or {}),
            extra_volumes=dict(parsed.get("extra_volumes") or {}),
            gpu_type=parsed.get("gpu_type") or "",
        ).normalized()

    def apply_to_baseline(self, baseline: Baseline) -> None:
        """Write the config onto a Baseline row (identity fields untouched)."""
        baseline.engine = self.engine
        baseline.image = self.image
        baseline.model_path = self.model_path
        baseline.service_port = self.service_port
        baseline.engine_args = dict(self.engine_args)
        baseline.extra_env = dict(self.extra_env)
        baseline.extra_volumes = dict(self.extra_volumes)

    def as_campaign_fields(self) -> dict[str, Any]:
        """The fields a new campaign form is prefilled with — the knobs
        become the search space's fixed base."""
        return {
            "engine": self.engine,
            "image": self.image,
            "model_path": self.model_path,
            "served_model_name": self.served_model_name,
            "service_port": self.service_port or 28200,
            "extra_env": dict(self.extra_env),
            "extra_volumes": dict(self.extra_volumes),
            "search_space": {"base": dict(self.engine_args), "grid": {}},
        }
