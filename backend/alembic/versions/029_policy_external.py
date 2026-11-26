"""A policy the platform does not run: no container, a token on request.

Revision ID: 029_policy_external
Revises: 029_cluster_card_request
"""

import sqlalchemy as sa

from alembic import op

revision = "029_policy_external"
down_revision = "029_cluster_card_request"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 001_initial creates today's models on a fresh database.
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("policies")}
    if "external" not in columns:
        op.add_column(
            "policies",
            sa.Column("external", sa.Boolean, nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    op.drop_column("policies", "external")
