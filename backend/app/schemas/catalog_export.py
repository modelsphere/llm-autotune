"""model-catalog export: request and response."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CatalogVariantIn(BaseModel):
    """One config to publish as a variant: a measured run (a campaign's
    baseline, its winner, or any other run with a benchmark result)."""

    run_id: int
    role: str = ""  # baseline | optimized | ""
    # A human name for the provenance lines; the campaign's name and the run
    # id when empty.
    label: str = ""
    id: str = ""  # variant id; derived (<engine>-<parallelism>-<card>-<role>) when empty
    description: str = ""  # derived from the measurement when empty
    # Exact nvidia.com/gpu.product values (e.g. NVIDIA-H100-80GB-HBM3). The
    # platform keeps only the normalized card name, so this is never guessed.
    gpu_product: list[str] = Field(default_factory=list)
    nodes: int | None = None  # pods per group; 1 when empty
    # The tag to publish when the run's image was named by digest alone.
    image_tag: str = ""


class CatalogExportRequest(BaseModel):
    variants: list[CatalogVariantIn] = Field(min_length=1)
    # The catalog's name for the model. A model the catalog already has keeps
    # its own; for a new one, empty = derived from the served model name.
    name: str = ""
    served_name: str = ""
    chart_version: str = ""  # empty = the engine's minimum, as a range
    default_variant: str = ""
    include_env: bool = True
    include_volumes: bool = False
    # Empty = 90 minutes, raised to 3x the measured load when that is longer.
    startup_minutes: int | None = Field(default=None, ge=1, le=240)
    # nvidia.com/gpu.product labels for every variant without its own; empty =
    # the known label of the card each config was measured on.
    gpu_product: list[str] = Field(default_factory=list)
    # metadata.yaml for a model new to the catalog. source.hf is required then:
    # every site finds the weights by it.
    hf: str = ""
    hf_revision: str = ""
    display_name: str = ""
    description: str = ""
    family: str = ""
    tags: list[str] = Field(default_factory=list)
    license: str = ""
    homepage: str = ""
    # A saved perf report (either language) to ship next to the files. It must
    # compare exactly the runs being exported; a tuning result needs one.
    report_id: int | None = None
    # Look the model up in the published catalog (catalog_index_url).
    use_catalog: bool = True
    # Env var name -> recipe | site | secret | platform, for this export only;
    # PUT /catalog-export/env-categories/{name} settles one for every export.
    env_categories: dict[str, str] = Field(default_factory=dict)


class CatalogFileOut(BaseModel):
    path: str
    kind: str  # variant | metadata | report | validation
    text: str


class CatalogInputOut(BaseModel):
    """One env var or mount of the measured run, and what the export did."""

    kind: str  # env | mount
    name: str  # the env var, or the mount's path in the container
    value: str  # the env value, or the host path
    category: (
        str  # recipe | site | secret | platform | unknown | weights | model | config | scratch
    )
    action: str  # kept | left out | emptyDir | moved
    reason: str


class CatalogVariantOut(BaseModel):
    id: str
    version: str
    role: str
    status: str  # new | updated | unchanged
    path: str
    inputs: list[CatalogInputOut] = Field(default_factory=list)
    # A tuned variant's result: recorded | no_report | no_baseline; "" otherwise.
    tuning: str = ""
    uplift: float | None = None


class EnvCategoryIn(BaseModel):
    category: Literal["recipe", "site", "secret", "platform"]


class EnvCategoryOut(BaseModel):
    name: str
    category: str
    set_by: str = ""


class CatalogExportOut(BaseModel):
    """The files to add under the catalog's models/<name>/, and what the form
    should show for fields the caller left empty."""

    name: str
    # The catalog already has this model: its metadata is updated, not created.
    existing: bool = False
    # found | new | unreachable: what the catalog's index said about the model.
    catalog: str = "new"
    variants: list[CatalogVariantOut] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    files: list[CatalogFileOut] = Field(default_factory=list)
    # What someone should look at before publishing.
    warnings: list[str] = Field(default_factory=list)
    # What the export did that needs no action.
    details: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    valid: bool
    # Where each variant came from, for the pull request description.
    provenance: list[str] = Field(default_factory=list)
    # A source.hf read off the weights path, to confirm; never written.
    hf_suggestion: str = ""
    # Where the perf report came from: "saved:<report id>"; "" when none ships.
    report_source: str = ""
    hf: str = ""
    display_name: str = ""
    description: str = ""
    family: str = ""
    tags: list[str] = Field(default_factory=list)
