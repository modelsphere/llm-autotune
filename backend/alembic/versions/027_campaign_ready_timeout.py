"""How long a campaign's engines may take to come up, per campaign.

Revision ID: 027_campaign_ready_timeout
Revises: 026_campaign_recipe_source
"""

import sqlalchemy as sa

from alembic import op

revision = "027_campaign_ready_timeout"
down_revision = "026_campaign_recipe_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 001_initial creates today's models on a fresh database.
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("campaigns")}
    if "ready_timeout_minutes" not in columns:
        op.add_column(
            "campaigns",
            sa.Column("ready_timeout_minutes", sa.Integer, nullable=False, server_default="120"),
        )


def downgrade() -> None:
    op.drop_column("campaigns", "ready_timeout_minutes")
