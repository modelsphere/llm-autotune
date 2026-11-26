"""Files every run of a campaign mounts into its engine (the base config's files).

Revision ID: 035_campaign_files
Revises: 034_run_files
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "035_campaign_files"
down_revision = "034_run_files"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 001_initial creates today's models on a fresh database.
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("campaigns")}
    if "files" not in columns:
        op.add_column(
            "campaigns",
            sa.Column(
                "files",
                sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
                nullable=False,
                server_default="{}",
            ),
        )


def downgrade() -> None:
    op.drop_column("campaigns", "files")
