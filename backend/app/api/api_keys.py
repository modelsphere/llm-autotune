"""Service credentials for machine-to-machine callers.

Minted by a logged-in user, who stays the owner: a key acts with its owner's
authority, and revoking the key is how you take that back. The plaintext is
returned by exactly one endpoint, exactly once.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import apikeys
from app.core.auth import get_current_user
from app.db.base import get_async_session
from app.db.models import ApiKey, Event, User

router = APIRouter(prefix="/api-keys", tags=["api keys"])


class ApiKeyCreate(BaseModel):
    name: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="What this key is for, e.g. 'fleet-manager'. Appears in the "
        "audit trail as `key:<name>` on everything the key does.",
    )


class ApiKeyOut(BaseModel):
    """A key as it can be shown after creation — identified, never readable."""

    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    prefix: str
    masked: str
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class ApiKeyCreated(ApiKeyOut):
    secret: str = Field(
        ...,
        description="The full key. Shown once and never recoverable — only its "
        "hash is stored. Losing it means minting a new one.",
    )


def _out(key: ApiKey) -> dict:
    return {
        "id": key.id,
        "name": key.name,
        "prefix": key.prefix,
        "masked": apikeys.masked(key.prefix),
        "last_used_at": key.last_used_at,
        "revoked_at": key.revoked_at,
        "created_at": key.created_at,
    }


@router.get(
    "",
    response_model=list[ApiKeyOut],
    summary="List API keys",
    description="Every key ever minted, revoked ones included — a revoked key is "
    "part of the audit record, so it is hidden from nobody.",
)
async def list_keys(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    keys = (
        (await session.execute(select(ApiKey).order_by(ApiKey.id.desc()))).scalars().all()
    )
    return [_out(k) for k in keys]


@router.post(
    "",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Mint an API key",
    description="Returns the plaintext key **once**. Store it wherever the calling "
    "service reads its config; it cannot be retrieved again.",
)
async def create_key(
    body: ApiKeyCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    secret, prefix, key_hash = apikeys.mint()
    key = ApiKey(name=body.name.strip(), prefix=prefix, key_hash=key_hash, owner_id=user.id)
    session.add(key)
    await session.flush()
    session.add(
        Event(
            actor=user.username,
            kind="api_key_created",
            # The name and prefix, never the secret: an audit log that could
            # authenticate is a second copy of the credential.
            payload={"name": key.name, "prefix": prefix},
        )
    )
    await session.commit()
    await session.refresh(key)
    return {**_out(key), "secret": secret}


@router.delete(
    "/{key_id}",
    response_model=ApiKeyOut,
    summary="Revoke an API key",
    description="Takes effect on the next request the key makes. The row is kept so "
    "past actions stay attributable.",
)
async def revoke_key(
    key_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    key = await session.get(ApiKey, key_id)
    if key is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such key")
    if key.revoked_at is None:
        key.revoked_at = datetime.now(UTC)
        session.add(
            Event(
                actor=user.username,
                kind="api_key_revoked",
                payload={"name": key.name, "prefix": key.prefix},
            )
        )
        await session.commit()
        await session.refresh(key)
    return _out(key)
