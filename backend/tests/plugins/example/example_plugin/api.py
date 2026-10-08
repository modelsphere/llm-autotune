"""The example plugin's routes, mounted under /api/example."""

from app.plugin_api import Campaign, User, get_async_session, get_current_user
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from example_plugin.models import CampaignLabel, Setting, TickNote

router = APIRouter(prefix="/example", tags=["example plugin"])


class LabelIn(BaseModel):
    label: str = Field(min_length=1, max_length=64)


@router.get("/ticks")
async def ticks(
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
) -> dict:
    count = await session.scalar(select(func.count()).select_from(TickNote))
    last = await session.scalar(select(TickNote).order_by(TickNote.id.desc()).limit(1))
    return {"ticks": count, "active_campaigns": last.active_campaigns if last else None}


@router.get("/settings/{key}")
async def setting(
    key: str,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
) -> dict:
    row = await session.get(Setting, key)
    if row is None:
        raise HTTPException(404, f"no setting {key!r}")
    return {"key": row.key, "value": row.value}


@router.put("/campaigns/{campaign_id}/label")
async def put_label(
    campaign_id: int,
    body: LabelIn,
    session: AsyncSession = Depends(get_async_session),
    user: User = Depends(get_current_user),
) -> dict:
    if await session.get(Campaign, campaign_id) is None:
        raise HTTPException(404, f"no campaign {campaign_id}")
    row = await session.get(CampaignLabel, campaign_id)
    if row is None:
        row = CampaignLabel(campaign_id=campaign_id, label=body.label)
        session.add(row)
    else:
        row.label = body.label
    await session.commit()
    return {"campaign_id": campaign_id, "label": row.label}
