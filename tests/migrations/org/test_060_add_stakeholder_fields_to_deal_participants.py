from __future__ import annotations

from pathlib import Path

MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[3] / "migrations" / "org_migrations" / "versions"
)
MIGRATION_060 = MIGRATIONS_DIR / "3c7f79637739_060_add_stakeholder_fields_to_deal_participants.py"


def test_060_keeps_participant_schema_change_simple() -> None:
    migration_text = MIGRATION_060.read_text(encoding="utf-8")

    assert "participant_dedupe" not in migration_text
    assert "row_number() OVER" not in migration_text
    assert "UPDATE crm_source_object_mappings AS mappings" not in migration_text
    assert "ux_crm_deal_participant_active_contact_local_id" not in migration_text
    assert "ux_crm_deal_participant_active_contact_email" not in migration_text
    assert "lower(btrim(related_email))" not in migration_text
    assert "ix_crm_deal_participant_contact_lookup" in migration_text


def test_060_adds_primary_contact_flag_without_repair_sql() -> None:
    migration_text = MIGRATION_060.read_text(encoding="utf-8")

    assert '"is_primary_contact"' in migration_text
    assert "sa.Boolean()" in migration_text
    assert "server_default=sa.text(\"false\")" in migration_text
