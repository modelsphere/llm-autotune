"""CI routing sidecar: which track and run each exported variant came from.

Catalog CI reads the deployment config from the variant files and uses this to
send new optimized variants back to the tracks they were measured on. Written
only when a plugin groups every exported run into a track. The catalog's files
and schema are unchanged by it.

Each variant file is bound by the SHA-256 of its content as canonical JSON
(sorted keys, no spaces), not of its bytes, so reformatting the YAML (the
catalog formats it on commit) does not break the binding.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Any

import yaml

from app.control.catalog_export import CatalogExport, CatalogFile, VariantSource

SCHEMA_VERSION = 2


def content_sha256(text: str) -> str:
    """The hash of a YAML document's content, whatever its formatting."""
    doc = yaml.safe_load(text)
    canonical = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validation_file(export: CatalogExport, sources: list[VariantSource]) -> CatalogFile | None:
    if not export.valid:
        return None
    if any(not source.origin.get("track_slug") for source in sources):
        export.details.append("no CI routing file: not every run belongs to a track")
        return None
    texts = {f.path: f.text for f in export.files if f.kind == "variant"}
    variants: list[dict[str, Any]] = []
    for variant, source in zip(export.variants, sources, strict=True):
        entry = {
            "variant_id": variant.id,
            "version": variant.version,
            "track_slug": source.origin["track_slug"],
            "source_run_id": source.origin["run_id"],
            # Only an explicit role routes: CI submits optimized variants.
            "role": variant.role if variant.role in ("baseline", "optimized") else "other",
        }
        if variant.path in texts:
            entry["file"] = PurePosixPath(variant.path).name
            entry["content_sha256"] = content_sha256(texts[variant.path])
        variants.append(entry)
    document = {"schema_version": SCHEMA_VERSION, "model": export.name, "variants": variants}
    return CatalogFile(
        path=f"models/{export.name}/{export.name}.validation.json",
        kind="validation",
        text=json.dumps(document, ensure_ascii=False, indent=2) + "\n",
    )
