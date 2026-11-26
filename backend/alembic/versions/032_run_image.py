"""The image a policy's launch runs, when it is not the campaign's (1.9).

Revision ID: 032_run_image
Revises: 031_run_progress
"""

import sqlalchemy as sa

from alembic import op

revision = "032_run_image"
down_revision = "031_run_progress"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 001_initial creates today's models on a fresh database.
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("runs")}
    if "image" not in columns:
        op.add_column("runs", sa.Column("image", sa.String(512), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("runs", "image")
