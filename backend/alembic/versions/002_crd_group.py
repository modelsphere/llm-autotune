"""TuningRun moves to the tuning.modelsphere.dev API group.

A cluster registered before this stores the old group and pod label as values,
so the platform would keep creating TuningRuns in a group the operator no
longer serves. Rewrite exactly the old defaults; a value someone set by hand to
something else is theirs and is left alone.

Revision ID: 002_crd_group
Revises: 001_initial
Create Date: 2026-09-24
"""
import sqlalchemy as sa

from alembic import op

revision = "002_crd_group"
down_revision = "001_initial"
branch_labels = None
depends_on = None

OLD_GROUP, NEW_GROUP = "tuning.llm-autotune.io", "tuning.modelsphere.dev"


def _rewrite(old_group: str, new_group: str) -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text("UPDATE clusters SET cr_group = :new WHERE cr_group = :old"),
        {"new": new_group, "old": old_group},
    )
    bind.execute(
        sa.text("UPDATE clusters SET cr_pod_label = :new WHERE cr_pod_label = :old"),
        {"new": f"{new_group}/run", "old": f"{old_group}/run"},
    )


def upgrade() -> None:
    _rewrite(OLD_GROUP, NEW_GROUP)


def downgrade() -> None:
    _rewrite(NEW_GROUP, OLD_GROUP)
