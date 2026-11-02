"""Existing clusters retain NVIDIA defaults; fresh schemas can run the migration."""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


@pytest.mark.parametrize("already_present", [False, True])
def test_product_label_upgrade_and_downgrade(already_present, monkeypatch):
    path = Path(__file__).parents[1] / "alembic/versions/019_cluster_gpu_product_label.py"
    spec = importlib.util.spec_from_file_location("product_label_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE clusters (id INTEGER PRIMARY KEY, name TEXT)")
        conn.exec_driver_sql("INSERT INTO clusters (id, name) VALUES (1, 'existing')")
        if already_present:
            conn.exec_driver_sql(
                "ALTER TABLE clusters ADD COLUMN gpu_product_label VARCHAR(253) "
                "NOT NULL DEFAULT 'nvidia.com/gpu.product'"
            )
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(conn)))
        migration.upgrade()
        assert conn.exec_driver_sql(
            "SELECT gpu_product_label FROM clusters WHERE id = 1"
        ).scalar_one() == "nvidia.com/gpu.product"
        migration.downgrade()
        columns = {c["name"] for c in sa.inspect(conn).get_columns("clusters")}
        assert "gpu_product_label" not in columns
    engine.dispose()
