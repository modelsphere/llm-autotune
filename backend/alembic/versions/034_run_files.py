"""Files a policy's launch mounts into its engine (1.9).

Revision ID: 034_run_files
Revises: 033_run_tainted
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "034_run_files"
down_revision = "033_run_tainted"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 001_initial creates today's models on a fresh database.
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("runs")}
    if "files" not in columns:
        op.add_column(
            "runs",
            sa.Column(
                "files",
                sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
                nullable=False,
                server_default="{}",
            ),
        )


def downgrade() -> None:
    op.drop_column("runs", "files")
