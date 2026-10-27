"""The configuration a campaign's runs start from, as a recipe.

Revision ID: 024_campaign_recipe
Revises: 023_cluster_llmservice
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "024_campaign_recipe"
down_revision = "023_cluster_llmservice"
branch_labels = None
depends_on = None

JsonType = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    # 001_initial creates today's models on a fresh database.
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("campaigns")}
    if "recipe" not in columns:
        op.add_column(
            "campaigns", sa.Column("recipe", JsonType, nullable=False, server_default="{}")
        )


def downgrade() -> None:
    op.drop_column("campaigns", "recipe")
