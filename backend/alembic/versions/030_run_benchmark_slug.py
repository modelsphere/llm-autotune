"""A run that measures with a benchmark of its own (a policy's probe).

Revision ID: 030_run_benchmark_slug
Revises: 029_policy_external
"""

import sqlalchemy as sa

from alembic import op

revision = "030_run_benchmark_slug"
down_revision = "029_policy_external"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 001_initial creates today's models on a fresh database.
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("runs")}
    if "benchmark_slug" not in columns:
        op.add_column(
            "runs",
            sa.Column("benchmark_slug", sa.String(100), nullable=False, server_default=""),
        )


def downgrade() -> None:
    op.drop_column("runs", "benchmark_slug")
