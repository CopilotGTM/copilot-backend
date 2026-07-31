"""Contracts for the additive tenant-row identity migration."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from scripts.maintenance.audit_shared_tenant_readiness import _organisation_tables

EXPECTED_TABLES = (
    "calendar_event_attendees",
    "calendar_event_user_states",
    "chat_message_attachment_link",
    "chat_message_jobs",
    "chat_messages",
    "integration_sync_history",
    "knowledge_base",
    "knowledge_processing_jobs",
    "transcript_segments",
)
EXPECTED_DEFAULT = "NULLIF(current_setting('app.current_organisation_id', true), '')::uuid"
MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "migrations"
    / "org_migrations"
    / "versions"
    / "3c7f79637717_082_add_missing_organisation_identity.py"
)
HELPER_PATH = (
    Path(__file__).resolve().parents[3] / "migrations" / "org_migrations" / "tenant_identity.py"
)


def _load_module(path: Path, name: str) -> ModuleType:
    assert path.exists(), f"missing module: {path.name}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_models_declare_non_null_indexed_tenant_identity() -> None:
    """All Task 1 findings must become fail-closed tenant-owned rows."""

    tables = {table.name: table for table in _organisation_tables()}

    for table_name in EXPECTED_TABLES:
        table = tables[table_name]
        assert "organisation_id" in table.c, table_name
        column = table.c.organisation_id
        assert column.nullable is False, table_name
        assert str(column.server_default.arg) == EXPECTED_DEFAULT, table_name
        assert any(
            tuple(index.columns) and tuple(index.columns)[0].name == "organisation_id"
            for index in table.indexes
        ), table_name


def test_migration_advances_head_and_owns_exact_audit_inventory() -> None:
    """Revision 082 must address only the nine tables proven by Task 1."""

    migration = _load_module(MIGRATION_PATH, "org_migration_082")

    assert migration.revision == "3c7f79637717"
    assert migration.down_revision == "3c7f79637718"
    assert migration.TENANT_IDENTITY_TABLES == EXPECTED_TABLES
    assert migration.TENANT_ORGANISATION_ID_DEFAULT_SQL == EXPECTED_DEFAULT


def test_legacy_schema_encodes_its_backfill_identity() -> None:
    """A selected legacy schema supplies its own canonical organisation UUID."""

    helper = _load_module(HELPER_PATH, "tenant_identity")
    schema_name = "org_11111111_2222_3333_4444_555555555555"

    assert helper.validate_migration_schema_name("tenant_data") == "tenant_data"
    assert str(helper.legacy_schema_organisation_id(schema_name)) == (
        "11111111-2222-3333-4444-555555555555"
    )


def test_migration_backfills_legacy_rows_before_hardening_identity() -> None:
    """Legacy rows receive their schema identity before NOT NULL is validated."""

    assert MIGRATION_PATH.exists(), f"missing migration: {MIGRATION_PATH.name}"
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    add_position = source.index("op.add_column")
    backfill_position = source.index("SET organisation_id = :organisation_id")
    validate_position = source.index("VALIDATE CONSTRAINT")
    not_null_position = source.index("nullable=False")
    default_position = source.index("server_default=sa.text")

    assert add_position < backfill_position < validate_position
    assert validate_position < not_null_position < default_position
    assert "legacy_schema_organisation_id" in source
    assert 'x_args.get("organisation_id")' not in source


def test_migration_links_identity_to_master_organisation() -> None:
    """Tenant rows must reference the existing master ownership record."""

    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert "op.create_foreign_key" in source
    assert '"organisations"' in source
    assert 'referent_schema="public"' in source
    assert 'ondelete="RESTRICT"' in source


def test_alembic_environment_never_creates_target_schema() -> None:
    """Terraform owns schema lifecycle; Alembic owns only schema contents."""

    env_path = MIGRATION_PATH.parents[1] / "env.py"
    source = env_path.read_text(encoding="utf-8")

    assert "CREATE SCHEMA" not in source
