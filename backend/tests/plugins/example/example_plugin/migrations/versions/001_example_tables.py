"""The example plugin's tables.

Revision ID: example_001
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "example_001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "example_tick_notes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("active_campaigns", sa.Integer(), nullable=False),
    )
    op.create_table(
        "example_campaign_labels",
        sa.Column(
            "campaign_id",
            sa.Integer(),
            sa.ForeignKey("campaigns.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("label", sa.String(64), nullable=False),
    )
    op.create_table(
        "example_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.String(255), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("example_settings")
    op.drop_table("example_campaign_labels")
    op.drop_table("example_tick_notes")
