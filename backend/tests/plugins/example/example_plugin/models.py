"""The example plugin's tables, on its own metadata, never the platform's."""

from datetime import datetime

from app.plugin_api import Campaign
from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TickNote(Base):
    """What one worker tick saw."""

    __tablename__ = "example_tick_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    active_campaigns: Mapped[int] = mapped_column(Integer)


class CampaignLabel(Base):
    """A plugin's data about a platform row lives in a side table keyed by the
    platform's primary key, never in a column added to the platform's table."""

    __tablename__ = "example_campaign_labels"

    campaign_id: Mapped[int] = mapped_column(
        ForeignKey(Campaign.id, ondelete="CASCADE"), primary_key=True
    )
    label: Mapped[str] = mapped_column(String(64))


class Setting(Base):
    __tablename__ = "example_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))
