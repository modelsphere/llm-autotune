"""model configs: a model's own config.json and the facts read from it

Revision ID: 022_model_configs
Revises: 021_engine_vocabularies

Read from the weights where a campaign's runs mount them, so a policy knows
the model it tunes: MoE and active experts, built-in draft layers, attention
layout, precision, context (control/engines/model_facts.py).
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "022_model_configs"
down_revision = "021_engine_vocabularies"
branch_labels = None
depends_on = None

JsonType = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    # 001_initial creates today's models on a fresh database.
    if "model_configs" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "model_configs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("model_path", sa.String(1024), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="ok"),
        sa.Column("facts", JsonType, nullable=False),
        sa.Column("config", JsonType, nullable=False),
        sa.Column("notes", JsonType, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_model_configs_model_path", "model_configs", ["model_path"])


def downgrade() -> None:
    op.drop_index("ix_model_configs_model_path", "model_configs")
    op.drop_table("model_configs")
