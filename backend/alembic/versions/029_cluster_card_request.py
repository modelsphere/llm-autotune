"""Per-cluster scheduler and card request name for LLMService runs.

Revision ID: 029_cluster_card_request
Revises: 028_queue_tickets
"""

import sqlalchemy as sa

from alembic import op

revision = "029_cluster_card_request"
down_revision = "028_queue_tickets"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("scheduler_name", sa.String(253), ""),
    ("gpu_request_resource", sa.String(253), ""),
)


def upgrade() -> None:
    # 001_initial creates today's models on a fresh database.
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("clusters")}
    for name, kind, default in _COLUMNS:
        if name not in columns:
            op.add_column(
                "clusters", sa.Column(name, kind, nullable=False, server_default=default)
            )


def downgrade() -> None:
    for name, _, _ in reversed(_COLUMNS):
        op.drop_column("clusters", name)
