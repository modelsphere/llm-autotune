"""Where a delegated policy pod runs, per cluster.

Revision ID: 020_cluster_policy_node_selector
Revises: 019_cluster_gpu_product_label
"""

import sqlalchemy as sa

from alembic import op

revision = "020_cluster_policy_node_selector"
down_revision = "019_cluster_gpu_product_label"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 001_initial creates today's models on a fresh database.
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("clusters")}
    if "policy_node_selector" not in columns:
        op.add_column(
            "clusters",
            sa.Column("policy_node_selector", sa.String(512), nullable=False, server_default=""),
        )


def downgrade() -> None:
    op.drop_column("clusters", "policy_node_selector")
