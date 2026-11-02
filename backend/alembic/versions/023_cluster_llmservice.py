"""Per-cluster settings for launching runs as LLMServices.

Revision ID: 023_cluster_llmservice
Revises: 022_model_configs
"""

import sqlalchemy as sa

from alembic import op

revision = "023_cluster_llmservice"
down_revision = "022_model_configs"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("chart_repo", sa.String(512), ""),
    ("chart_credentials_secret", sa.String(253), ""),
    ("endpoint", sa.String(16), "dns"),
    ("image_mirror", sa.String(255), ""),
    ("site_values", sa.Text(), ""),
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
