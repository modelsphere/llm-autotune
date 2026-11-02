"""Where a campaign's recipe came from (a catalog variant).

Revision ID: 026_campaign_recipe_source
Revises: 025_campaign_chart_version
"""

import sqlalchemy as sa

from alembic import op

revision = "026_campaign_recipe_source"
down_revision = "025_campaign_chart_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 001_initial creates today's models on a fresh database.
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("campaigns")}
    if "recipe_source" not in columns:
        op.add_column(
            "campaigns",
            sa.Column("recipe_source", sa.String(255), nullable=False, server_default=""),
        )


def downgrade() -> None:
    op.drop_column("campaigns", "recipe_source")
