"""Recipe: one engine configuration, as the chart and the catalog read it.

A recipe is the serving part of a model-catalog variant: which chart, at which
version range, and the chart values that make the model run. It is what a
tuning campaign starts from, what every trial launches, and what a winner is
published as, so the configuration the platform measured and the one a
deploy renders are the same document rather than two that a converter keeps
in step.

`values` is the variant's `catalog` layer as swiss composes it, projections
included: `image.repository`/`image.tag`, `model.name` (the served name) and
`model.gpus` live inside it, where the chart reads them. The variant file
spells those three as `image`, `servedName` and `requires.gpus` instead;
`to_variant` and `from_variant` move them across, and nothing else changes.

What a recipe never holds is where it runs: the weights' host path, the
namespace, scheduling, replicas, routing. Those are the site's and the run's
layers (compose.py), and the checks refuse them here (check.py).
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from pydantic import BaseModel, Field

from app.control.recipe import args as argv

ENGINES = ("sglang", "vllm")
ROLES = ("prefill", "decode")


class ChartRef(BaseModel):
    name: str = "sglang"
    # An exact version or a range in helm's constraint syntax, as the catalog
    # writes it. A launch resolves it once per campaign (compose.py).
    version: str = ""


class Recipe(BaseModel):
    engine: str = "sglang"
    chart: ChartRef = Field(default_factory=ChartRef)
    values: dict[str, Any] = Field(default_factory=dict)
    # The image digest the configuration was measured on, when known. Not a
    # chart value: the charts name an image by repository and tag.
    image_digest: str = ""

    # -- the projected fields ------------------------------------------------

    @property
    def image(self) -> str:
        image = self.values.get("image") or {}
        repository, tag = image.get("repository") or "", image.get("tag") or ""
        ref = f"{repository}:{tag}" if tag else repository
        return f"{ref}@{self.image_digest}" if self.image_digest else ref

    @property
    def served_name(self) -> str:
        return str((self.values.get("model") or {}).get("name") or "")

    @property
    def gpus(self) -> int:
        raw = (self.values.get("model") or {}).get("gpus")
        try:
            return int(str(raw).strip() or 0)
        except ValueError:
            return 0

    def flags(self, role: str = "") -> dict[str, Any]:
        """The engine flags this recipe sets. With a role, the flags that
        role's engines run with: the top-level ones, then the role's own, the
        later winning (the chart's own order)."""
        top = argv.flags(self.engine, self.values.get("extraArgs"))
        if not role:
            return top
        own = argv.flags(self.engine, self.role(role).get("extraArgs"))
        return {**top, **own}

    def env(self) -> dict[str, str]:
        """The engine env as name → value. An entry that takes its value from a
        secret or field ref reads as "" here; its source is kept in `values`."""
        out: dict[str, str] = {}
        for entry in self.values.get("env") or []:
            if isinstance(entry, dict) and entry.get("name"):
                out[str(entry["name"])] = "" if "value" not in entry else str(entry["value"])
        return out

    def role(self, name: str) -> dict[str, Any]:
        return ((self.values.get("pd") or {}).get(name)) or {}

    @property
    def topology(self) -> str:
        """single-node | lws | pd, as the catalog's `requires.topology` names it."""
        if (self.values.get("pd") or {}).get("enabled"):
            return "pd"
        if (self.values.get("lws") or {}).get("enabled"):
            return "lws"
        return "single-node"

    # -- identity ------------------------------------------------------------

    def identity(self) -> dict[str, Any]:
        """What makes two recipes the same configuration: the chart and every
        value, with the lists whose spelling varies (flags, env) read as what
        they mean. The chart's version range and the digest are not identity:
        a range is a statement about compatibility, and the digest a fact
        about one build of the same tag."""
        values = copy.deepcopy(self.values)
        values["extraArgs"] = self.flags()
        values["env"] = dict(sorted(self.env().items()))
        for role in ROLES:
            spec = (values.get("pd") or {}).get(role)
            if isinstance(spec, dict) and "extraArgs" in spec:
                spec["extraArgs"] = argv.flags(self.engine, spec.get("extraArgs"))
        return {"engine": self.engine, "chart": self.chart.name, "values": values}

    def hash(self) -> str:
        text = json.dumps(self.identity(), sort_keys=True, separators=(",", ":"), default=str)
        return "sha256:" + hashlib.sha256(text.encode()).hexdigest()

    # -- the catalog's spelling ----------------------------------------------

    def to_variant(self) -> dict[str, Any]:
        """The variant fields this recipe fills: engine, chart, image,
        servedName, requires.gpus and values. The rest of a variant (id,
        version, description, requires.gpuProduct...) belongs to whoever
        publishes it."""
        values = copy.deepcopy(self.values)
        image = values.pop("image", None) or {}
        model = values.get("model") or {}
        served = model.pop("name", None)
        gpus = model.pop("gpus", None)
        if not model and "model" in values:
            values.pop("model")
        out: dict[str, Any] = {
            "engine": self.engine,
            "chart": {"name": self.chart.name, "version": self.chart.version},
        }
        if served:
            out["servedName"] = served
        if image.get("repository"):
            out["image"] = {"repository": image["repository"], "tag": str(image.get("tag") or "")}
            if self.image_digest:
                out["image"]["digest"] = self.image_digest
        requires: dict[str, Any] = {"gpus": int(str(gpus)) if gpus not in (None, "") else 0}
        nodes = int((values.get("lws") or {}).get("size") or 1) if self.topology == "lws" else 1
        if nodes > 1:
            requires["nodes"] = nodes
        requires["topology"] = self.topology
        if (values.get("rdma") or {}).get("enabled"):
            requires["rdma"] = True
        out["requires"] = requires
        out["values"] = values
        return out

    @classmethod
    def from_variant(cls, variant: dict[str, Any], model_name: str = "") -> Recipe:
        """A catalog variant as a recipe: the inverse of `to_variant`, with the
        projections swiss's compose step applies (servedName falling back to
        the model's name, as the catalog does)."""
        values = copy.deepcopy(variant.get("values") or {})
        image = variant.get("image") or {}
        if image.get("repository"):
            values["image"] = {
                "repository": image["repository"],
                "tag": str(image.get("tag") or ""),
            }
        model = values.setdefault("model", {})
        served = variant.get("servedName") or model_name or variant.get("model") or ""
        if served:
            model["name"] = served
        gpus = (variant.get("requires") or {}).get("gpus")
        if gpus is not None:
            model["gpus"] = str(gpus)
        chart = variant.get("chart") or {}
        return cls(
            engine=variant.get("engine") or chart.get("name") or "sglang",
            chart=ChartRef(
                name=chart.get("name") or "sglang", version=str(chart.get("version") or "")
            ),
            values=values,
            image_digest=str(image.get("digest") or ""),
        )
