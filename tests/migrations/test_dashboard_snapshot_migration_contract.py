from pathlib import Path


def _dashboard_snapshot_migration_source() -> str:
    migrations_dir = Path(__file__).resolve().parents[2] / "migrations" / "org_migrations"
    migration_path = migrations_dir / "versions" / "3c7f79637729_070_add_dashboard_snapshots.py"
    return migration_path.read_text(encoding="utf-8")


def test_dashboard_snapshot_migration_creates_tenant_dashboards_table() -> None:
    source = _dashboard_snapshot_migration_source()

    assert '"dashboards"' in source
    assert '"dashboard_id"' in source
    assert '"organisation_id"' in source
    assert '"dashboard_key"' in source
    assert '"definition"' in source
    assert '"latest_data"' in source
    assert '"latest_generation_status"' in source
    assert "ux_dashboards_org_key_active" in source
    assert "ix_dashboards_refresh_lookup" in source
