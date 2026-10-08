"""The example plugin's tick step."""

from app.plugin_api import Campaign, CampaignStatus, Supervisor
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from example_plugin.models import TickNote


def note_active_campaigns(supervisor: Supervisor, session: Session) -> None:
    active = session.scalar(
        select(func.count())
        .select_from(Campaign)
        .where(Campaign.status == CampaignStatus.ACTIVE.value)
    )
    session.add(TickNote(active_campaigns=active or 0))
