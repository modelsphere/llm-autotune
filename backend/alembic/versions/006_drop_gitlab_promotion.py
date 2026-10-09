"""drop the GitLab merge-request path

Revision ID: 006_drop_gitlab_promotion
Revises: 005_drop_production_capture

A campaign's winner is no longer proposed to a deploy repository: the
promotion records, the baselines' bindings to a repo file, and a campaign's
release branch and auto-promote switch go. A run keeps its exact launch
command and image, which is what a person applies.
"""

import sqlalchemy as sa

from alembic import op

revision = "006_drop_gitlab_promotion"
down_revision = "005_drop_production_capture"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    for table in ("promotions", "deploy_bindings"):
        if table in tables:
            op.drop_table(table)
    columns = {c["name"] for c in inspector.get_columns("campaigns")}
    with op.batch_alter_table("campaigns") as batch:
        for column in ("deploy_branch", "auto_promote"):
            if column in columns:
                batch.drop_column(column)


def downgrade() -> None:
    raise NotImplementedError("the GitLab promotion path is not restored by a downgrade")
