"""A launch a policy ran a command in is tainted (1.9).

Revision ID: 033_run_tainted
Revises: 032_run_image
"""

import sqlalchemy as sa

from alembic import op

revision = "033_run_tainted"
down_revision = "032_run_image"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 001_initial creates today's models on a fresh database.
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("runs")}
    if "tainted" not in columns:
        op.add_column(
            "runs",
            sa.Column("tainted", sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    op.drop_column("runs", "tainted")
