"""Engines and sessions.

The API uses the async engine; the orchestrator worker deliberately uses a
plain sync engine (its work is subprocess ssh + HTTP polling in a tick loop —
async buys nothing there and complicates re-attach logic).
"""

from collections.abc import AsyncGenerator

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()

async_engine = create_async_engine(_settings.database_url, pool_pre_ping=True)
async_session_factory = async_sessionmaker(async_engine, expire_on_commit=False)

sync_engine = create_engine(_settings.sync_database_url, pool_pre_ping=True)
sync_session_factory = sessionmaker(sync_engine, expire_on_commit=False)


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        yield session


def get_sync_session() -> Session:
    return sync_session_factory()
