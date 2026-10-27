"""engine vocabularies: the flags and env vars an engine image reads

Revision ID: 021_engine_vocabularies
Revises: 020_cluster_policy_node_selector

Dumped from the campaign's image, one row per digest, so a policy proposes
switches the engine it runs actually reads (control/engines/vocabulary.py).
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "021_engine_vocabularies"
down_revision = "020_cluster_policy_node_selector"
branch_labels = None
depends_on = None

JsonType = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    # 001_initial creates today's models on a fresh database.
    if "engine_vocabularies" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "engine_vocabularies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("engine", sa.String(16), nullable=False),
        sa.Column("image", sa.String(512), nullable=False),
        sa.Column("image_digest", sa.String(128), nullable=False, server_default=""),
        sa.Column("engine_version", sa.String(64), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="ok"),
        sa.Column("entries", JsonType, nullable=False),
        sa.Column("notes", JsonType, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_engine_vocabularies_image", "engine_vocabularies", ["image"])


def downgrade() -> None:
    op.drop_index("ix_engine_vocabularies_image", "engine_vocabularies")
    op.drop_table("engine_vocabularies")
