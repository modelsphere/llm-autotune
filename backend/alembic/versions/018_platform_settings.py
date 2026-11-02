"""platform settings and LLM endpoints: the admin Settings page

Revision ID: 018_platform_settings
Revises: 017_policy_session_digest

External platforms' addresses (VictoriaLogs, the model catalog server and its
write key) were install-time env only; `platform_settings` lets an admin set
them in the UI, the env staying the fallback. `llm_endpoints` registers each
LLM endpoint once, for report writing and for agent policies, which name one
with `policies.llm_endpoint_id` instead of carrying a key of their own.
"""

import sqlalchemy as sa

from alembic import op

revision = "018_platform_settings"
down_revision = "017_policy_session_digest"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 001_initial materializes today's models, so a database created by this
    # chain from empty already has these.
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "llm_endpoints" not in tables:
        op.create_table(
            "llm_endpoints",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(128), nullable=False, unique=True),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("base_url", sa.String(512), nullable=False),
            sa.Column("model", sa.String(255), nullable=False),
            sa.Column("api_key", sa.Text(), nullable=False, server_default=""),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
            ),
        )
    if "platform_settings" not in tables:
        op.create_table(
            "platform_settings",
            sa.Column("key", sa.String(64), primary_key=True),
            sa.Column("value", sa.Text(), nullable=False, server_default=""),
            sa.Column("secret", sa.Text(), nullable=False, server_default=""),
            sa.Column(
                "updated_by_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
            ),
        )
    columns = {c["name"] for c in inspector.get_columns("policies")}
    if "llm_endpoint_id" not in columns:
        op.add_column(
            "policies",
            sa.Column(
                "llm_endpoint_id",
                sa.Integer(),
                sa.ForeignKey("llm_endpoints.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )


def downgrade() -> None:
    op.drop_column("policies", "llm_endpoint_id")
    op.drop_table("platform_settings")
    op.drop_table("llm_endpoints")
