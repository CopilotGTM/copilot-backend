from __future__ import annotations

from datetime import UTC, datetime
from typing import get_type_hints
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from src.domain.models.organisation_tables.sync.crm import (
    CRMActivity,
    CRMActivityEmailDisplayPayload,
    CRMActivityLink,
    CRMActivityMeetingDisplayPayload,
    CRMNote,
    CRMSourceObjectMapping,
    CRMTask,
    validate_crm_activity_display_payload,
)


def _index_names(model: type) -> set[str]:
    return {
        arg.name
        for arg in getattr(model, "__table_args__", ())
        if isinstance(arg, Index) and arg.name is not None
    }


def _index_by_name(model: type, name: str) -> Index:
    for arg in getattr(model, "__table_args__", ()):
        if isinstance(arg, Index) and arg.name == name:
            return arg
    raise AssertionError(f"{name} index not found on {model.__name__}")


def _unique_constraint_columns(model: type, name: str) -> tuple[str, ...]:
    for arg in getattr(model, "__table_args__", ()):
        if isinstance(arg, UniqueConstraint) and arg.name == name:
            return tuple(column.name for column in arg.columns)
    raise AssertionError(f"{name} unique constraint not found on {model.__name__}")


def _foreign_key_constraint_by_columns(
    model: type,
    column_names: list[str],
) -> ForeignKeyConstraint:
    for constraint in model.__table__.constraints:
        if not isinstance(constraint, ForeignKeyConstraint):
            continue
        if [element.parent.name for element in constraint.elements] == column_names:
            return constraint
    raise AssertionError(f"FK on {column_names} not found on {model.__name__}")


def _check_constraint_names(model: type) -> set[str]:
    return {
        arg.name
        for arg in getattr(model, "__table_args__", ())
        if isinstance(arg, CheckConstraint) and arg.name is not None
    }


def _check_constraint_sql(model: type, name: str) -> str:
    for arg in getattr(model, "__table_args__", ()):
        if isinstance(arg, CheckConstraint) and arg.name == name:
            return str(arg.sqltext)
    raise AssertionError(f"{name} check constraint not found on {model.__name__}")


def _foreign_key_targets(model: type, column_name: str) -> set[str]:
    return {str(foreign_key.column) for foreign_key in model.__table__.c[column_name].foreign_keys}


def test_ai_native_activity_tables_are_named_for_their_storage_roles() -> None:
    assert CRMTask.__tablename__ == "crm_tasks"
    assert CRMNote.__tablename__ == "crm_notes"
    assert CRMActivity.__tablename__ == "crm_activities"
    assert CRMActivityLink.__tablename__ == "crm_activity_links"


def test_ai_native_activity_feed_fields_are_separate_from_full_task_and_note_storage() -> None:
    activity_columns = CRMActivity.__table__.columns.keys()
    task_columns = CRMTask.__table__.columns.keys()
    note_columns = CRMNote.__table__.columns.keys()

    assert {
        "occurred_at",
        "title",
        "summary",
        "preview",
        "source_type",
        "source_record_id",
    }.issubset(activity_columns)
    assert {"due_at", "reminder_at", "assigned_to_user_id"}.issubset(task_columns)
    assert {"body", "body_format", "author_user_id", "visibility"}.issubset(note_columns)


def test_activity_feed_orm_does_not_map_columns_retired_by_migration_066() -> None:
    """Prevent ORM reads from selecting activity columns that no longer exist."""
    # Migration 066 removed task/note detail from the feed projection. Mapping any
    # retired name makes every select(CRMActivity) fail before rows can be returned.
    retired_columns = {
        "body",
        "status",
        "priority",
        "activity_subtype",
        "owner_name",
        "owner_email",
        "owner_id",
        "external_assignee_email",
        "external_assignee_name",
        "source",
        "created_by_user_id",
        "due_date",
        "end_date",
        "completed_date",
        "reminder_date",
        "duration_minutes",
        "attendees",
        "direction",
        "call_outcome",
        "source_created_at",
        "source_modified_at",
        "synced_at",
        "last_seen_at",
        "is_all_day",
        "properties",
        "metrics",
    }
    retired_indexes = {
        "ix_crm_activity_org_status",
        "ix_crm_activity_org_priority",
        "ix_crm_activity_due_date",
        "ix_crm_activity_org_due",
        "ix_crm_activity_status_due",
        "ix_crm_activity_synced",
        "ix_crm_activity_source",
        "ix_crm_activity_created_by",
        "ix_crm_activity_task_owner_due",
        "ix_crm_activity_task_created_by_due",
        "ix_crm_activity_task_external_email_due",
    }

    assert retired_columns.isdisjoint(CRMActivity.__table__.columns.keys())
    assert retired_indexes.isdisjoint(_index_names(CRMActivity))


def test_task_state_fields_have_database_guards() -> None:
    assert {"ck_crm_task_status", "ck_crm_task_priority"}.issubset(_check_constraint_names(CRMTask))
    assert CRMTask.__table__.c.status.nullable is False
    assert CRMTask.__table__.c.priority.nullable is False


def test_source_mapping_allows_note_link_for_salesforce_content_document_links() -> None:
    external_sql = _check_constraint_sql(
        CRMSourceObjectMapping,
        "ck_crm_source_mapping_external_type",
    )
    local_sql = _check_constraint_sql(
        CRMSourceObjectMapping,
        "ck_crm_source_mapping_local_type",
    )

    assert "'note_link'" in external_sql
    assert "'note_link'" in local_sql


def test_task_and_note_source_tables_have_required_defaults_for_provider_writes() -> None:
    assert CRMTask.__table__.c.title.nullable is False
    assert CRMTask.__table__.c.status.nullable is False
    assert CRMTask.__table__.c.status.server_default is not None
    assert CRMTask.__table__.c.priority.nullable is False
    assert CRMTask.__table__.c.priority.server_default is not None
    assert CRMTask.__table__.c.task_metadata.nullable is False
    assert CRMTask.__table__.c.task_metadata.server_default is not None

    assert CRMNote.__table__.c.body.nullable is False
    assert CRMNote.__table__.c.body_format.nullable is False
    assert CRMNote.__table__.c.body_format.server_default is not None
    assert CRMNote.__table__.c.visibility.nullable is False
    assert CRMNote.__table__.c.visibility.server_default is not None
    assert CRMNote.__table__.c.note_metadata.nullable is False
    assert CRMNote.__table__.c.note_metadata.server_default is not None


def test_task_people_and_due_date_fields_are_explicit_without_premature_ai_provenance() -> None:
    task_columns = CRMTask.__table__.columns.keys()

    assert {
        "created_by_user_id",
        "owner_user_id",
        "assigned_to_user_id",
        "completed_by_user_id",
    }.issubset(task_columns)
    assert {"due_date", "due_at", "is_all_day", "reminder_at"}.issubset(task_columns)
    assert "due_timezone" not in task_columns
    assert not {
        "creation_source",
        "ai_agent_name",
        "ai_tool_name",
        "ai_approval_state_id",
        "ai_evidence_activity_ids",
    }.intersection(task_columns)


def test_activity_source_record_id_is_local_source_row_pointer() -> None:
    assert isinstance(CRMActivity.__table__.c.source_record_id.type, Text)
    assert "local source row ID" in CRMActivity.__table__.c.source_record_id.comment


def test_activities_expose_org_activity_key_for_link_tenant_isolation() -> None:
    assert _unique_constraint_columns(CRMActivity, "uq_crm_activity_org_activity") == (
        "organisation_id",
        "activity_id",
    )


def test_long_text_fields_match_migration_text_columns() -> None:
    assert isinstance(CRMActivity.__table__.c.summary.type, Text)
    assert isinstance(CRMTask.__table__.c.description.type, Text)
    assert isinstance(CRMNote.__table__.c.body.type, Text)


def test_tasks_and_notes_have_direct_deal_context_for_fast_deal_reads() -> None:
    task_columns = CRMTask.__table__.columns.keys()
    note_columns = CRMNote.__table__.columns.keys()

    assert "deal_id" in task_columns
    assert "deal_id" in note_columns
    assert _foreign_key_targets(CRMTask, "deal_id") == {"crm_deals.deal_id"}
    assert _foreign_key_targets(CRMNote, "deal_id") == {"crm_deals.deal_id"}
    assert "ix_crm_task_deal" in _index_names(CRMTask)
    assert "ix_crm_note_deal" in _index_names(CRMNote)


def test_deal_context_indexes_are_partial_for_active_deal_scoped_rows() -> None:
    task_index = _index_by_name(CRMTask, "ix_crm_task_deal")
    note_index = _index_by_name(CRMNote, "ix_crm_note_deal")

    assert str(task_index.dialect_options["postgresql"]["where"]) == (
        "deal_id IS NOT NULL AND is_deleted IS NOT TRUE"
    )
    assert str(note_index.dialect_options["postgresql"]["where"]) == (
        "deal_id IS NOT NULL AND is_deleted IS NOT TRUE"
    )


def test_internal_tasks_and_notes_do_not_require_integrations() -> None:
    assert CRMTask.__table__.c.integration_id.nullable is True
    assert CRMNote.__table__.c.integration_id.nullable is True


def test_task_and_note_rows_do_not_carry_provider_identity() -> None:
    task_columns = CRMTask.__table__.columns.keys()
    note_columns = CRMNote.__table__.columns.keys()

    assert not {"source_provider", "source_record_id"}.intersection(task_columns)
    assert not {"source_provider", "source_record_id"}.intersection(note_columns)
    assert "uq_crm_task_source_identity" not in _index_names(CRMTask)
    assert "uq_crm_note_source_identity" not in _index_names(CRMNote)


def test_note_format_and_visibility_have_database_guards() -> None:
    assert {"ck_crm_note_body_format", "ck_crm_note_visibility"}.issubset(
        _check_constraint_names(CRMNote)
    )
    assert CRMNote.__table__.c.body_format.nullable is False
    assert CRMNote.__table__.c.visibility.nullable is False


def test_activity_links_are_indexed_for_object_feed_reads_in_time_order() -> None:
    link_columns = CRMActivityLink.__table__.columns.keys()
    assert {"object_type", "object_id", "activity_id", "occurred_at"}.issubset(link_columns)
    assert "ix_crm_activity_links_object_time" in _index_names(CRMActivityLink)
    assert CRMActivityLink.__table__.c.link_role.nullable is False
    assert _unique_constraint_columns(CRMActivityLink, "uq_crm_activity_link_target_role") == (
        "organisation_id",
        "activity_id",
        "object_type",
        "object_id",
        "link_role",
    )

    activity_fk = _foreign_key_constraint_by_columns(
        CRMActivityLink,
        ["organisation_id", "activity_id"],
    )
    assert activity_fk.ondelete == "CASCADE"
    assert [str(element.column) for element in activity_fk.elements] == [
        "crm_activities.organisation_id",
        "crm_activities.activity_id",
    ]


def test_activity_link_feed_timestamp_must_be_explicit() -> None:
    occurred_at = CRMActivityLink.__table__.c.occurred_at

    assert occurred_at.nullable is False
    assert occurred_at.default is None
    assert occurred_at.server_default is None


def test_activity_links_do_not_keep_a_second_primary_flag() -> None:
    assert "is_primary" not in CRMActivityLink.__table__.columns


def test_activity_link_classification_fields_have_database_guards() -> None:
    assert {
        "ck_crm_activity_link_object_type",
        "ck_crm_activity_link_role",
        "ck_crm_activity_link_source",
    }.issubset(_check_constraint_names(CRMActivityLink))
    assert "LEAD" in _check_constraint_sql(
        CRMActivityLink,
        "ck_crm_activity_link_object_type",
    )


def test_activity_link_required_strings_are_not_optional_in_python() -> None:
    type_hints = get_type_hints(CRMActivityLink)

    assert type_hints["link_role"] is str
    assert type_hints["link_source"] is str


def test_activity_feed_does_not_store_unresearched_ai_signals() -> None:
    assert "importance_score" not in CRMActivity.__table__.columns
    assert "sentiment" not in CRMActivity.__table__.columns
    assert "ck_crm_activity_importance_score_range" not in _check_constraint_names(CRMActivity)


def test_activity_feed_has_common_display_snapshot_fields() -> None:
    activity_columns = CRMActivity.__table__.columns

    assert {"actor_name", "actor_email", "display_payload"}.issubset(activity_columns.keys())
    assert "actor_user_id" not in activity_columns
    assert "ix_crm_activity_actor_user" not in _index_names(CRMActivity)
    assert isinstance(activity_columns["display_payload"].type, JSONB)
    assert activity_columns["display_payload"].nullable is False
    assert activity_columns["display_payload"].server_default is not None
    assert "timeline display snapshot" in activity_columns["display_payload"].comment


def test_activity_display_payload_shape_is_validated_by_pydantic_not_database() -> None:
    constraint_names = _check_constraint_names(CRMActivity)

    assert not {
        "ck_crm_activity_display_payload_object",
        "ck_crm_activity_display_payload_common_shape",
        "ck_crm_activity_display_payload_email_shape",
        "ck_crm_activity_display_payload_meeting_shape",
    }.intersection(constraint_names)


def test_email_display_payload_has_typed_pydantic_contract() -> None:
    association_id = uuid4()
    payload = validate_crm_activity_display_payload(
        "EMAIL",
        {
            "association_id": association_id,
            "direction": "INBOUND",
            "participants_preview": [
                {"name": "Sarah Chen", "email": "sarah@acme.example", "role": "SENDER"}
            ],
            "participant_count": 1,
            "has_attachments": False,
        },
    )

    assert payload == {
        "association_id": str(association_id),
        "direction": "INBOUND",
        "participants_preview": [
            {"name": "Sarah Chen", "email": "sarah@acme.example", "role": "SENDER"}
        ],
        "participant_count": 1,
        "has_attachments": False,
    }
    with pytest.raises(ValidationError):
        CRMActivityEmailDisplayPayload.model_validate(
            {"participant_count": "one", "has_attachments": "false"}
        )


def test_meeting_display_payload_has_typed_pydantic_contract() -> None:
    association_id = uuid4()
    start_at = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    end_at = datetime(2026, 7, 1, 9, 45, tzinfo=UTC)
    payload = validate_crm_activity_display_payload(
        "MEETING",
        {
            "association_id": association_id,
            "start_at": start_at,
            "end_at": end_at,
            "participants_preview": [
                {"name": "Maya Singh", "email": "maya@copilotgtm.example", "role": "ORGANIZER"}
            ],
            "participant_count": 3,
            "duration_minutes": 45,
            "meeting_status": "COMPLETED",
        },
    )

    assert payload == {
        "association_id": str(association_id),
        "start_at": start_at.isoformat().replace("+00:00", "Z"),
        "end_at": end_at.isoformat().replace("+00:00", "Z"),
        "participants_preview": [
            {"name": "Maya Singh", "email": "maya@copilotgtm.example", "role": "ORGANIZER"}
        ],
        "participant_count": 3,
        "duration_minutes": 45,
        "meeting_status": "COMPLETED",
    }
    with pytest.raises(ValidationError):
        CRMActivityMeetingDisplayPayload.model_validate(
            {"duration_minutes": "45", "participants_preview": [{"name": "Maya"}] * 6}
        )


def test_activity_link_confidence_score_is_normalized_when_present() -> None:
    assert "confidence_score >= 0" in _check_constraint_sql(
        CRMActivityLink, "ck_crm_activity_link_confidence_score_range"
    )
