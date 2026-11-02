"""The model catalog, read for a campaign's starting point.

The platform's catalog (Settings, or AUTOTUNE_CATALOG_INDEX_URL) lists the
models it publishes and their variants; a variant's file read here is what
the create form puts in a campaign's recipe, unchanged, so a campaign tunes
exactly what production deploys.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.control.catalog_export import catalog_index
from app.core import platform_settings
from app.core.auth import get_current_user
from app.db.base import get_async_session
from app.db.models import User

router = APIRouter(prefix="/catalog", tags=["catalog"])


async def _index_url(db: AsyncSession) -> str:
    url = (await platform_settings.resolve(db)).catalog_index_url
    if not url:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "no model catalog is set (Settings, or AUTOTUNE_CATALOG_INDEX_URL)",
        )
    return url


@router.get("/models", summary="Models the catalog publishes, with their variants")
async def catalog_models(
    db: AsyncSession = Depends(get_async_session), _: User = Depends(get_current_user)
):
    url = await _index_url(db)
    try:
        return {"index": url, "models": await catalog_index.models(url)}
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"could not read the catalog at {url}: {exc}"
        ) from exc


@router.get(
    "/models/{name}/variants/{variant_id}", summary="One published variant file, as text"
)
async def catalog_variant(
    name: str,
    variant_id: str,
    db: AsyncSession = Depends(get_async_session),
    _: User = Depends(get_current_user),
):
    url = await _index_url(db)
    try:
        found = await catalog_index.variant(url, name, variant_id)
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"could not read the catalog at {url}: {exc}"
        ) from exc
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"the catalog has no {name}/{variant_id}")
    return found
