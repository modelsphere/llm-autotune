"""What the example plugin keeps about a campaign: its label.

A campaign is created with `extensions: {"example": {"label": "..."}}`;
`on_campaign_created` saves the label in the plugin's side table, and
`campaign_extensions` shows it back on the campaign (and so in its spec and
in a clone of it).
"""

from collections.abc import Sequence
from typing import Any

from app.plugin_api import Campaign, ExtensionRefused
from sqlalchemy import select
from sqlalchemy.orm import Session

from example_plugin.models import CampaignLabel


def on_campaign_created(session: Session, campaign: Campaign, data: dict[str, Any]) -> None:
    label = str(data.get("label", "")).strip()
    if not label or len(label) > 64:
        raise ExtensionRefused("example: label must be 1 to 64 characters")
    session.add(CampaignLabel(campaign_id=campaign.id, label=label))


def campaign_extensions(
    session: Session, campaigns: Sequence[Campaign]
) -> dict[int, dict[str, Any]]:
    rows = session.scalars(
        select(CampaignLabel).where(CampaignLabel.campaign_id.in_([c.id for c in campaigns]))
    ).all()
    return {row.campaign_id: {"label": row.label} for row in rows}
