from __future__ import annotations

from pathlib import Path


def test_crm_user_normalized_email_migration_adds_unique_org_guard() -> None:
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "org_migrations"
        / "versions"
        / "3c7f79637728_071_unique_crm_user_normalized_email.py"
    )
    source = migration_path.read_text(encoding="utf-8")

    assert 'revision: str = "3c7f79637728"' in source
    assert 'down_revision: str | None = "3c7f79637729"' in source
    assert "CREATE UNIQUE INDEX IF NOT EXISTS ux_crm_users_org_normalized_email" in source
    assert "ON crm_users (organisation_id, lower(btrim(email)))" in source
    assert "WHERE email IS NOT NULL AND btrim(email) <> ''" in source
    assert "DROP INDEX IF EXISTS ux_crm_users_org_normalized_email" in source
