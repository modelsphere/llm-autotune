"""What the model catalog already publishes, read from its index.

model-catalog's site publishes `index-v2.json`: every model with its metadata
and each variant's id, version and path at a commit. The export looks a model
up there, by Hugging Face repo id first (a different id is a different model),
then by name, and reads the files it needs raw at that commit: metadata.yaml,
and the variant files, to tell a re-export of a published variant from a
change to it.

Read once per export, never stored: the catalog changes when pull requests
merge, and an export reflects it as it is now. An index that cannot be read is
not an error; the export says so and treats the model as new.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import yaml

from app.control.catalog_export import CatalogModel

TIMEOUT = httpx.Timeout(20.0, connect=10.0)


ATTEMPTS = 3


async def _get(client: httpx.AsyncClient, url: str) -> httpx.Response:
    """A read that rides out a dropped connection or a 5xx: a file that could
    not be read makes a re-exported variant look changed, which would publish
    a version for nothing."""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            res = await client.get(url, follow_redirects=True)
            if res.status_code < 500 or attempt == ATTEMPTS:
                res.raise_for_status()
                return res
        except httpx.TransportError:
            if attempt == ATTEMPTS:
                raise
        await asyncio.sleep(0.5 * attempt)
    raise AssertionError("unreachable")


def find(index: dict[str, Any], name: str, hf: str) -> dict[str, Any] | None:
    models = index.get("models") or []
    if hf:
        for m in models:
            if ((m.get("source") or {}).get("hf") or "").lower() == hf.lower():
                return m
    return next((m for m in models if m.get("name") == name), None)


async def lookup(
    url: str, name: str, hf: str, transport: httpx.AsyncBaseTransport | None = None
) -> tuple[CatalogModel | None, str]:
    """The model as the catalog publishes it, or None, and a note for the
    export's warnings when the index could not be read."""
    if not url:
        return None, ""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport) as client:
            index = (await _get(client, url)).json()
            model = find(index, name, hf)
            if model is None:
                return None, ""
            raw = str(index.get("raw") or "").rstrip("/")
            commit = str(index.get("commit") or "master")
            name = model["name"]

            async def text(path: str) -> str:
                try:
                    return (await _get(client, f"{raw}/{commit}/{path}")).text
                except httpx.HTTPError:
                    return ""

            variants = model.get("variants") or []
            meta_text, *variant_texts = await asyncio.gather(
                text(f"models/{name}/metadata.yaml"),
                *(text(v.get("path") or f"models/{name}/{v.get('id')}.yaml") for v in variants),
            )
    except (httpx.HTTPError, ValueError) as exc:
        return None, f"could not read the catalog's index ({exc})"

    metadata = _load(meta_text) or {
        k: v for k, v in model.items() if k not in ("variants", "reports")
    }
    metadata.setdefault("apiVersion", index.get("apiVersion") or "catalog.swiss/v2")
    found = CatalogModel(name=name, metadata=metadata, metadata_text=meta_text)
    for v, body in zip(variants, variant_texts, strict=True):
        found.variants[str(v.get("id"))] = {
            "version": str(v.get("version") or ""),
            "document": _load(body),
        }
    return found, ""


def _load(text: str) -> dict[str, Any] | None:
    try:
        doc = yaml.safe_load(text) if text else None
    except yaml.YAMLError:
        return None
    return doc if isinstance(doc, dict) else None
