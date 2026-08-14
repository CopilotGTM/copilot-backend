"""Deployment-startup contract for the permanent shared tenant schema."""

from pathlib import Path


def test_startup_migrates_only_the_permanent_tenant_schema() -> None:
    """A deployment must never revisit per-organisation migration targets."""

    startup_text = Path("scripts/startup.sh").read_text(encoding="utf-8")

    assert "python -m scripts.run_org_migrations --schema tenant_data" in startup_text
    assert "python -m scripts.run_org_migrations --all" not in startup_text


def test_startup_uses_the_dedicated_migration_port_and_validates_the_catalog() -> None:
    """DDL must bypass PgBouncer and pass the exact runtime gate before rollout."""

    startup_text = Path("scripts/startup.sh").read_text(encoding="utf-8")

    port_override = 'export DATABASE_PORT="$MIGRATION_DATABASE_PORT"'
    master_migration = "python -m scripts.run_master_migrations"
    assert port_override in startup_text
    assert startup_text.index(port_override) < startup_text.index(master_migration)
    assert "unset DATABASE_URL" in startup_text
    assert "validate_current_tenant_runtime_catalog" in startup_text
