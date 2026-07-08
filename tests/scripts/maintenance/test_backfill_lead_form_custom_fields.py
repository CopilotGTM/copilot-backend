from __future__ import annotations

from scripts.maintenance import backfill_lead_form_custom_fields as backfill


def test_backfill_script_selects_one_candidate_per_normalized_key() -> None:
    preview = backfill.preview_sql()

    assert "lead_form_fields.target_type = 'property'" in preview
    assert "lead_form_fields.data_type <> 'json'" in preview
    assert "row_number() OVER" in preview
    assert "PARTITION BY candidates.normalized_key" in preview
    assert "display_order NULLS LAST" in preview
    assert "already_exists" in preview


def test_backfill_script_inserts_lead_definitions_without_migration_marker() -> None:
    insert = backfill.insert_sql()

    assert "INSERT INTO crm_custom_field_definitions" in insert
    assert "'lead'" in insert
    assert "'internal'" in insert
    assert "RETURNING key" in insert
    assert "lead_form_property_backfill" not in insert
    assert "data_payload" not in insert


def test_backfill_script_reports_json_and_invalid_keys() -> None:
    skipped = backfill.skipped_counts_sql()

    assert "skipped_json" in skipped
    assert "skipped_invalid_key" in skipped
    assert "^[a-z][a-z0-9_]" in skipped
