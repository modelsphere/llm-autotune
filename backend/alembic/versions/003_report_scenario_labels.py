"""reports: what the report calls its scenarios

Revision ID: 003_report_scenario_labels
Revises: 002_crd_group

A scenario goes by its token shape by default ("Input 50k / output 1.5k"),
which says what ran but not what it stands for. A report may now name its
scenarios ("Agent long-context") and say what each one simulates; the names
are per report, so each language carries its own, and every chart and table
drawn from the frozen comparison uses them.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "003_report_scenario_labels"
down_revision = "002_crd_group"
branch_labels = None
depends_on = None

JsonType = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    # 001_initial materializes today's models, so a database created by this
    # chain from empty already has the column; one created by 0.1.x does not.
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("agent_reports")}
    if "scenario_labels" not in columns:
        op.add_column("agent_reports", sa.Column("scenario_labels", JsonType,
                                                 nullable=False, server_default="{}"))


def downgrade() -> None:
    op.drop_column("agent_reports", "scenario_labels")
