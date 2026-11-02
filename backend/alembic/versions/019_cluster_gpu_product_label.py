"""Configurable accelerator product label per cluster.

Revision ID: 019_cluster_gpu_product_label
Revises: 018_platform_settings
"""

import sqlalchemy as sa

from alembic import op

revision = "019_cluster_gpu_product_label"
down_revision = "018_platform_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 001_initial creates today's models on a fresh database.
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("clusters")}
    if "gpu_product_label" not in columns:
        op.add_column(
            "clusters",
            sa.Column(
                "gpu_product_label", sa.String(253), nullable=False,
                server_default="nvidia.com/gpu.product",
            ),
        )


def downgrade() -> None:
    op.drop_column("clusters", "gpu_product_label")
