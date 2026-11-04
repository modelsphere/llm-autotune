"""The machine queue's tickets, and what a queued run waits for and why.

Revision ID: 028_queue_tickets
Revises: 027_campaign_ready_timeout
"""

import sqlalchemy as sa

from alembic import op

revision = "028_queue_tickets"
down_revision = "027_campaign_ready_timeout"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 001_initial creates today's models on a fresh database.
    inspector = sa.inspect(op.get_bind())
    columns = {c["name"] for c in inspector.get_columns("runs")}
    if "waiting_for" not in columns:
        op.add_column(
            "runs", sa.Column("waiting_for", sa.String(16), nullable=False, server_default="")
        )
    if "waiting_reason" not in columns:
        op.add_column(
            "runs", sa.Column("waiting_reason", sa.Text, nullable=False, server_default="")
        )
    if "queue_tickets" not in inspector.get_table_names():
        op.create_table(
            "queue_tickets",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("waiter", sa.String(96), nullable=False, unique=True),
            sa.Column("label", sa.String(255), nullable=False, server_default=""),
            sa.Column(
                "campaign_id", sa.Integer,
                sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=True,
            ),
            sa.Column("arrival", sa.DateTime(timezone=True), nullable=False),
            sa.Column("rank", sa.Integer, nullable=False, server_default="0"),
            sa.Column("ident", sa.Integer, nullable=False, server_default="0"),
            sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("position", sa.Integer, nullable=False, server_default="0"),
            sa.Column("waiting_for", sa.String(16), nullable=False, server_default=""),
            sa.Column("reason", sa.Text, nullable=False, server_default=""),
        )
        op.create_index("ix_queue_tickets_campaign_id", "queue_tickets", ["campaign_id"])


def downgrade() -> None:
    op.drop_index("ix_queue_tickets_campaign_id", "queue_tickets")
    op.drop_table("queue_tickets")
    op.drop_column("runs", "waiting_reason")
    op.drop_column("runs", "waiting_for")
