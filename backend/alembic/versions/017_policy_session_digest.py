"""policy session digest: what a night learned, for other campaigns

Revision ID: 017_policy_session_digest
Revises: 016_run_engine_incarnation

A policy's state is per campaign, so a second campaign on the same model
starts knowing nothing. `policy_sessions.digest` holds the night's findings in
the policy's words (PUT /session/digest); GET /history serves them to other
campaigns with the conditions and validated verdicts the platform recorded.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

JsonType = sa.JSON().with_variant(JSONB(), "postgresql")

revision = "017_policy_session_digest"
down_revision = "016_run_engine_incarnation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 001_initial materializes today's models, so a database created by this
    # chain from empty already has the columns.
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("policy_sessions")}
    if "digest" in columns:
        return
    op.add_column("policy_sessions", sa.Column("digest", JsonType, nullable=True))
    op.add_column(
        "policy_sessions", sa.Column("digest_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("policy_sessions", "digest_at")
    op.drop_column("policy_sessions", "digest")
