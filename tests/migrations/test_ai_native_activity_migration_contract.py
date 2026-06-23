from __future__ import annotations

from pathlib import Path


def test_ai_native_activity_migration_creates_source_and_link_tables() -> None:
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "org_migrations"
        / "versions"
        / "3c7f79637735_064_ai_native_crm_activity_tables.py"
    )
    source = migration_path.read_text(encoding="utf-8")

    assert 'revision: str = "3c7f79637735"' in source
    assert 'down_revision: str | None = "3c7f79637736"' in source
    assert '"crm_tasks"' in source
    assert '"crm_notes"' in source
    assert '"crm_activity_links"' in source
    assert "ix_crm_activity_links_object_time" in source
    assert "occurred_at" in source
    assert 'sa.Column("source_record_id", sa.Text(), nullable=True)' in source
    assert 'sa.Column("integration_id", sa.UUID(), nullable=True)' in source
    assert "uq_crm_activity_link_target_role" in source
    assert 'sa.Column("actor_name", sa.String(length=255), nullable=True)' in source
    assert 'sa.Column("actor_email", sa.String(length=255), nullable=True)' in source
    assert '"display_payload"' in source
    assert "postgresql.JSONB(astext_type=sa.Text())" in source
    assert "server_default=sa.text(\"'{}'::jsonb\")" in source
    assert "timeline display snapshot" in source
    assert "ck_crm_activity_display_payload_object" not in source
    assert "ck_crm_activity_display_payload_common_shape" not in source
    assert "ck_crm_activity_display_payload_email_shape" not in source
    assert "ck_crm_activity_display_payload_meeting_shape" not in source
    assert "jsonb_typeof(display_payload)" not in source
    assert '"actor_user_id"' not in source
    assert "fk_crm_activities_actor_user_id" not in source
    assert "ix_crm_activity_actor_user" not in source
    assert "external_task_id" not in source
    assert "external_note_id" not in source
    assert "uq_crm_task_source_identity" not in source
    assert "uq_crm_note_source_identity" not in source
    assert "ix_crm_task_source" not in source
    assert "ix_crm_note_source" not in source
    assert "ck_crm_task_status" in source
    assert "ck_crm_task_priority" in source
    assert "ck_crm_note_body_format" in source
    assert "ck_crm_note_visibility" in source
    assert "importance_score" not in source
    assert "sentiment" not in source
    assert "ck_crm_activity_importance_score_range" not in source
    assert "ck_crm_activity_link_confidence_score_range" in source
    assert "ck_crm_activity_link_object_type" in source
    assert "ck_crm_activity_link_role" in source
    assert "ck_crm_activity_link_source" in source
    assert "uq_crm_activity_org_activity" in source
    assert (
        'sa.ForeignKeyConstraint(\n'
        '            ["organisation_id", "activity_id"],\n'
        '            ["crm_activities.organisation_id", "crm_activities.activity_id"],\n'
        '            ondelete="CASCADE",\n'
        '        )'
    ) in source
    assert 'ondelete="CASCADE"' in source
    assert "LEAD" in source
    assert '"is_primary"' not in source
    assert 'sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False)' in source
    assert 'sa.Column("deal_id", sa.UUID(), nullable=True)' in source
    assert 'sa.ForeignKeyConstraint(["deal_id"], ["crm_deals.deal_id"])' in source
    assert "ix_crm_task_deal" in source
    assert "ix_crm_note_deal" in source
    assert source.count(
        'postgresql_where=sa.text("deal_id IS NOT NULL AND is_deleted IS NOT TRUE")'
    ) == 2
    assert 'sa.Column("due_date", sa.Date(), nullable=True)' in source
    assert "due_timezone" not in source
    assert 'sa.Column("is_all_day", sa.Boolean(), nullable=False' in source
    assert 'sa.Column("completed_by_user_id", sa.UUID(), nullable=True)' in source
    assert "creation_source" not in source
    assert "ai_agent_name" not in source
    assert "ai_tool_name" not in source
    assert "ai_approval_state_id" not in source
    assert "ai_evidence_activity_ids" not in source


def test_ai_native_activity_migration_revision_is_not_duplicated() -> None:
    versions_dir = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "org_migrations"
        / "versions"
    )
    revisions = [
        path
        for path in versions_dir.glob("*.py")
        if 'revision: str = "3c7f79637735"' in path.read_text(encoding="utf-8")
    ]

    assert [path.name for path in revisions] == [
        "3c7f79637735_064_ai_native_crm_activity_tables.py"
    ]


def test_source_mapping_migration_allows_note_link() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "org_migrations"
        / "versions"
        / "3c7f79637743_056_add_crm_source_object_mappings.py"
    ).read_text(encoding="utf-8")

    assert "'note_link'" in source
    assert "ck_crm_source_mapping_external_type" in source
    assert "ck_crm_source_mapping_local_type" in source


def test_ai_native_activity_docs_match_source_record_contract() -> None:
    specs_dir = Path(__file__).resolve().parents[2] / "docs" / "superpowers" / "specs"
    design_doc = (
        specs_dir / "2026-06-20-ai-native-crm-activity-model-design.md"
    ).read_text(encoding="utf-8")
    sample_json = (
        specs_dir / "2026-06-20-ai-native-crm-activity-sample.json"
    ).read_text(encoding="utf-8")

    assert "source_record_id text NOT NULL" not in design_doc
    assert "source_record_id text" in design_doc
    assert "local source row ID" in design_doc
    assert "local email row ID" in sample_json
    assert "local calendar event row ID" in sample_json
    assert "For v1, a task or note can belong to at most one deal" in design_doc
    assert "Activity link `occurred_at` must be copied from the source activity time" in design_doc
    assert '"is_primary"' not in sample_json


def test_ai_native_activity_backfill_migrates_legacy_tasks_and_notes() -> None:
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "org_migrations"
        / "versions"
        / "3c7f79637734_065_backfill_legacy_activity_tasks_notes.py"
    )
    source = migration_path.read_text(encoding="utf-8")

    assert 'revision: str = "3c7f79637734"' in source
    assert 'down_revision: str | None = "3c7f79637735"' in source
    assert "INSERT INTO crm_tasks" in source
    assert "INSERT INTO crm_notes" in source
    assert "UPDATE crm_source_object_mappings" in source
    assert "ON CONFLICT (task_id) DO NOTHING" in source
    assert "ON CONFLICT (note_id) DO NOTHING" in source
    assert "local_object_type" in source
    assert "task" in source
    assert "note" in source
    assert "UPDATE crm_activities" in source
    assert "source_record_id = activity_id::text" in source
    assert "source_type = 'TASK'" in source
    assert "source_type = 'NOTE'" in source
    assert "activity_type IN ('TASK', 'TODO')" in source
    assert "left(btrim(COALESCE(body, summary, preview, '')), 80)" in source
    assert "a.external_id" not in source
    assert "local_object_type = 'activity'" in source


def test_ai_native_activity_cleanup_drops_legacy_activity_columns_separately() -> None:
    migration_path = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "org_migrations"
        / "versions"
        / "3c7f79637733_066_drop_legacy_activity_task_note_columns.py"
    )
    source = migration_path.read_text(encoding="utf-8")

    assert 'revision: str = "3c7f79637733"' in source
    assert 'down_revision: str | None = "3c7f79637734"' in source
    assert "DROP INDEX IF EXISTS ix_crm_activity_task_owner_due" in source
    assert "DROP INDEX IF EXISTS ix_crm_activity_task_created_by_due" in source
    assert "DROP INDEX IF EXISTS ix_crm_activity_task_external_email_due" in source
    assert 'op.drop_column("crm_activities", "body")' in source
    assert 'op.drop_column("crm_activities", "status")' in source
    assert 'op.drop_column("crm_activities", "priority")' in source
    assert 'op.drop_column("crm_activities", "owner_id")' in source
    assert 'op.drop_column("crm_activities", "due_date")' in source
    assert 'op.drop_column("crm_activities", "properties")' in source
    assert "INSERT INTO crm_tasks" not in source
    assert "INSERT INTO crm_notes" not in source
