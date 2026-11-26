"""What a benchmark has measured so far, module by module, while it runs.

Revision ID: 031_run_progress
Revises: 030_run_benchmark_slug
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "031_run_progress"
down_revision = "030_run_benchmark_slug"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 001_initial creates today's models on a fresh database.
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("runs")}
    if "progress" not in columns:
        op.add_column(
            "runs",
            sa.Column(
                "progress", sa.JSON().with_variant(JSONB(), "postgresql"),
                nullable=False, server_default="{}",
            ),
        )


def downgrade() -> None:
    op.drop_column("runs", "progress")
