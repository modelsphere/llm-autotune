"""The chart version a campaign's runs render, pinned once.

Revision ID: 025_campaign_chart_version
Revises: 024_campaign_recipe
"""

import sqlalchemy as sa

from alembic import op

revision = "025_campaign_chart_version"
down_revision = "024_campaign_recipe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 001_initial creates today's models on a fresh database.
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("campaigns")}
    if "chart_version" not in columns:
        op.add_column(
            "campaigns",
            sa.Column("chart_version", sa.String(64), nullable=False, server_default=""),
        )


def downgrade() -> None:
    op.drop_column("campaigns", "chart_version")
