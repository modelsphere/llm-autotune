"""machines: the platform no longer captures, clears or restores production

Revision ID: 005_drop_production_capture
Revises: 004_campaign_run_timing

A lease is the hand-over: the machine is given to the platform free, and the
platform only stops what it launched itself. What a machine recorded about
production, and how far through capture/clear/restore it was, goes.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "005_drop_production_capture"
down_revision = "004_campaign_run_timing"
branch_labels = None
depends_on = None

JsonType = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("machines")}
    with op.batch_alter_table("machines") as batch:
        if "baseline" in columns:
            batch.drop_column("baseline")
        if "baseline_status" in columns:
            batch.drop_column("baseline_status")


def downgrade() -> None:
    with op.batch_alter_table("machines") as batch:
        batch.add_column(sa.Column("baseline", JsonType, nullable=False, server_default="{}"))
        batch.add_column(sa.Column("baseline_status", sa.String(16), nullable=False,
                                   server_default="none"))
