"""campaigns: what the runs actually took

Revision ID: 004_campaign_run_timing
Revises: 003_report_scenario_labels

A campaign was asked up front how long a run, a model startup and a validation
would take. The platform measures all three on every run, so it now keeps the
recent figures on the campaign and plans the window from them.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "004_campaign_run_timing"
down_revision = "003_report_scenario_labels"
branch_labels = None
depends_on = None

JsonType = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    # 001_initial materializes today's models, so a database created by this
    # chain from empty already has the column; one created by 0.1.x does not.
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("campaigns")}
    if "run_timing" not in columns:
        op.add_column("campaigns", sa.Column("run_timing", JsonType,
                                             nullable=False, server_default="{}"))


def downgrade() -> None:
    op.drop_column("campaigns", "run_timing")
