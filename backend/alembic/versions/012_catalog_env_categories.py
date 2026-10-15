"""catalog env categories: what a person settled an env var to be in exports

Revision ID: 012_catalog_env_categories
Revises: 011_campaign_owners

The model-catalog export sorts a measured config's env vars into the serving
recipe and the site's setup by rules. A name no rule knows is settled once by a
person; this table keeps that answer for every later export.
"""

import sqlalchemy as sa

from alembic import op

revision = "012_catalog_env_categories"
down_revision = "011_campaign_owners"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 001_initial materializes today's models, so a database created by this
    # chain from empty already has the table; one created by 0.3.x does not.
    if "catalog_env_categories" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "catalog_env_categories",
        sa.Column("name", sa.String(128), primary_key=True),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column(
            "set_by_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "set_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("catalog_env_categories")
