"""Prevent every remaining organisation relationship from crossing tenants.

Revision ID: 3c7f79637715
Revises: 3c7f79637716
Create Date: 2026-07-30

Revision 083 hardened CRM-owned child tables. This revision applies the same
database boundary to the remaining organisation models while deliberately
preserving global uniqueness for physical blob storage paths. Downgrade
restores legacy global uniqueness and therefore refuses data containing
identifiers that became valid duplicates across tenants after this upgrade.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from migrations.org_migrations.tenant_identity import validate_migration_schema_name
from sqlalchemy.dialects import postgresql

revision: str = "3c7f79637715"
down_revision: str | None = "3c7f79637716"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONVERSATION_INTELLIGENCE_TABLES = (
    "ci_calls",
    "ci_call_deal_links",
    "ci_call_speakers",
    "ci_transcript_segments",
    "ci_call_analytics",
)

# (parent table, parent identity column)
REMAINING_CANDIDATE_KEYS = (
    ("attachments", "attachment_id"),
    ("automation_triggers", "trigger_id"),
    ("calendar_event_attendees", "attendee_id"),
    ("calendar_events", "event_id"),
    ("chat_messages", "message_id"),
    ("chat_sessions", "session_id"),
    ("ci_call_speakers", "speaker_id"),
    ("ci_calls", "call_id"),
    ("email_folders", "folder_id"),
    ("integration_participants", "participant_id"),
    ("meetings", "meeting_id"),
    ("messaging_channels", "channel_id"),
    ("messaging_messages", "message_id"),
    ("organisation_sources", "source_id"),
    ("user_agents", "agent_id"),
    ("user_reminders", "reminder_id"),
)

# (child table, local identity, parent table, parent identity, ON DELETE)
REMAINING_TENANT_FOREIGN_KEYS = (
    ("agent_runs", "agent_id", "user_agents", "agent_id", "RESTRICT"),
    (
        "agent_runs",
        "trigger_id",
        "automation_triggers",
        "trigger_id",
        "RESTRICT",
    ),
    (
        "calendar_event_attendees",
        "event_id",
        "calendar_events",
        "event_id",
        None,
    ),
    (
        "calendar_event_deal_associations",
        "deal_id",
        "crm_deals",
        "deal_id",
        None,
    ),
    (
        "calendar_event_deal_associations",
        "event_id",
        "calendar_events",
        "event_id",
        None,
    ),
    (
        "calendar_event_user_states",
        "event_id",
        "calendar_events",
        "event_id",
        None,
    ),
    (
        "calendar_event_user_states",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    (
        "chat_message_attachment_link",
        "attachment_id",
        "attachments",
        "attachment_id",
        None,
    ),
    (
        "chat_message_attachment_link",
        "message_id",
        "chat_messages",
        "message_id",
        None,
    ),
    (
        "chat_message_jobs",
        "message_id",
        "chat_messages",
        "message_id",
        None,
    ),
    (
        "chat_messages",
        "parent_message_id",
        "chat_messages",
        "message_id",
        None,
    ),
    ("chat_messages", "session_id", "chat_sessions", "session_id", None),
    ("chat_session_access", "session_id", "chat_sessions", "session_id", None),
    ("ci_call_analytics", "call_id", "ci_calls", "call_id", None),
    (
        "ci_call_analytics",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    ("ci_call_deal_links", "call_id", "ci_calls", "call_id", None),
    ("ci_call_deal_links", "deal_id", "crm_deals", "deal_id", None),
    (
        "ci_call_deal_links",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    ("ci_call_speakers", "call_id", "ci_calls", "call_id", None),
    (
        "ci_call_speakers",
        "crm_contact_id",
        "crm_contacts",
        "contact_id",
        None,
    ),
    ("ci_call_speakers", "crm_user_id", "crm_users", "user_id", None),
    (
        "ci_call_speakers",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    (
        "ci_call_speakers",
        "participant_id",
        "integration_participants",
        "participant_id",
        None,
    ),
    ("ci_calls", "crm_owner_id", "crm_users", "user_id", None),
    (
        "ci_calls",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    ("ci_calls", "primary_deal_id", "crm_deals", "deal_id", None),
    ("ci_transcript_segments", "call_id", "ci_calls", "call_id", None),
    (
        "ci_transcript_segments",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    (
        "ci_transcript_segments",
        "speaker_id",
        "ci_call_speakers",
        "speaker_id",
        None,
    ),
    ("deal_assets", "deal_id", "crm_deals", "deal_id", None),
    (
        "deal_assets",
        "kb_source_id",
        "organisation_sources",
        "source_id",
        None,
    ),
    ("deal_email_associations", "deal_id", "crm_deals", "deal_id", None),
    ("deal_jobs", "deal_id", "crm_deals", "deal_id", None),
    ("deal_jobs", "event_id", "calendar_events", "event_id", None),
    ("deal_memories", "deal_id", "crm_deals", "deal_id", None),
    ("deal_risks_blockers", "deal_id", "crm_deals", "deal_id", None),
    (
        "deal_stakeholder_insights",
        "deal_id",
        "crm_deals",
        "deal_id",
        None,
    ),
    ("deal_timeline_events", "deal_id", "crm_deals", "deal_id", None),
    (
        "email_folders",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    ("email_thread_summaries", "deal_id", "crm_deals", "deal_id", None),
    (
        "email_thread_summaries",
        "folder_id",
        "email_folders",
        "folder_id",
        None,
    ),
    (
        "email_thread_summaries",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    ("emails", "deal_id", "crm_deals", "deal_id", None),
    ("emails", "folder_id", "email_folders", "folder_id", None),
    (
        "emails",
        "from_participant_id",
        "integration_participants",
        "participant_id",
        None,
    ),
    (
        "emails",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    (
        "integration_activity_requests",
        "deal_id",
        "crm_deals",
        "deal_id",
        None,
    ),
    (
        "integration_activity_requests",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    (
        "integration_participant_identities",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    (
        "integration_participant_identities",
        "participant_id",
        "integration_participants",
        "participant_id",
        None,
    ),
    (
        "integration_participants",
        "crm_contact_id",
        "crm_contacts",
        "contact_id",
        None,
    ),
    (
        "integration_participants",
        "crm_user_id",
        "crm_users",
        "user_id",
        None,
    ),
    (
        "integration_participants",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    (
        "integration_sync_history",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    (
        "integration_sync_states",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        "CASCADE",
    ),
    (
        "knowledge_base",
        "source_id",
        "organisation_sources",
        "source_id",
        None,
    ),
    (
        "knowledge_integrations",
        "deal_id",
        "crm_deals",
        "deal_id",
        None,
    ),
    (
        "knowledge_processing_jobs",
        "source_id",
        "organisation_sources",
        "source_id",
        None,
    ),
    (
        "meeting_recording_sessions",
        "calendar_event_id",
        "calendar_events",
        "event_id",
        None,
    ),
    ("meetings", "calendar_event_id", "calendar_events", "event_id", None),
    (
        "messaging_channel_integration_states",
        "channel_id",
        "messaging_channels",
        "channel_id",
        None,
    ),
    (
        "messaging_channel_integration_states",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    (
        "messaging_channel_members",
        "channel_id",
        "messaging_channels",
        "channel_id",
        None,
    ),
    (
        "messaging_deal_links",
        "channel_id",
        "messaging_channels",
        "channel_id",
        None,
    ),
    ("messaging_deal_links", "deal_id", "crm_deals", "deal_id", None),
    (
        "messaging_deal_links",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    (
        "messaging_deal_links",
        "message_id",
        "messaging_messages",
        "message_id",
        None,
    ),
    (
        "messaging_messages",
        "channel_id",
        "messaging_channels",
        "channel_id",
        None,
    ),
    (
        "messaging_messages",
        "parent_message_id",
        "messaging_messages",
        "message_id",
        None,
    ),
    (
        "messaging_thread_summaries",
        "channel_id",
        "messaging_channels",
        "channel_id",
        None,
    ),
    (
        "messaging_thread_summaries",
        "deal_id",
        "crm_deals",
        "deal_id",
        None,
    ),
    (
        "messaging_thread_summaries",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    (
        "organisation_sources",
        "integration_id",
        "knowledge_integrations",
        "integration_id",
        None,
    ),
    (
        "transcript_segments",
        "attendee_id",
        "calendar_event_attendees",
        "attendee_id",
        None,
    ),
    ("transcript_segments", "meeting_id", "meetings", "meeting_id", None),
    (
        "user_reminder_runs",
        "reminder_id",
        "user_reminders",
        "reminder_id",
        None,
    ),
)

# Only the three relationships that had no existing local-ID index.
REMAINING_FOREIGN_KEY_INDEXES = (
    ("chat_message_attachment_link", "attachment_id"),
    ("chat_messages", "parent_message_id"),
    ("deal_assets", "kb_source_id"),
)

# (table, kind, legacy name, scoped name, scoped columns, predicate,
#  deferrable, initially)
REMAINING_SCOPED_UNIQUE_RULES = (
    (
        "attachments",
        "index",
        "uq_attachments_whatsapp_source_external_id",
        "uq_attachments_org_whatsapp_source_external_id",
        ("organisation_id", "source_external_id"),
        "source_provider = 'whatsapp' AND source_external_id IS NOT NULL",
        False,
        None,
    ),
    (
        "calendar_event_attendees",
        "constraint",
        "uq_calendar_attendee_event",
        "uq_calendar_attendee_event_org",
        ("organisation_id", "event_id", "email"),
        None,
        False,
        None,
    ),
    (
        "calendar_event_deal_associations",
        "constraint",
        "uq_calendar_event_deal",
        "uq_calendar_event_deal_org",
        ("organisation_id", "event_id", "deal_id"),
        None,
        False,
        None,
    ),
    (
        "calendar_event_user_states",
        "constraint",
        "uq_calendar_event_user_state",
        "uq_calendar_event_user_state_org",
        ("organisation_id", "event_id", "integration_id"),
        None,
        False,
        None,
    ),
    (
        "calendar_events",
        "constraint",
        "uq_calendar_event_external_occurrence",
        "uq_calendar_event_external_occurrence_org",
        ("organisation_id", "external_id", "occurrence_anchor"),
        None,
        False,
        None,
    ),
    (
        "chat_message_jobs",
        "index",
        "uq_chat_message_jobs_channel_idempotency",
        "uq_chat_message_jobs_channel_idempotency_org",
        ("organisation_id", "chat_type", "idempotency_key"),
        "idempotency_key IS NOT NULL",
        False,
        None,
    ),
    (
        "ci_call_analytics",
        "constraint",
        "uq_ci_call_analysis_type",
        "uq_ci_call_analysis_type_org",
        ("organisation_id", "integration_id", "call_id", "analysis_type"),
        None,
        False,
        None,
    ),
    (
        "ci_call_deal_links",
        "constraint",
        "uq_ci_call_deal",
        "uq_ci_call_deal_org",
        ("organisation_id", "integration_id", "call_id", "deal_id"),
        None,
        False,
        None,
    ),
    (
        "ci_call_deal_links",
        "constraint",
        "uq_ci_call_deal_external",
        "uq_ci_call_deal_external_org",
        (
            "organisation_id",
            "integration_id",
            "call_id",
            "deal_external_id",
        ),
        None,
        False,
        None,
    ),
    (
        "ci_call_speakers",
        "constraint",
        "uq_ci_call_speaker_external",
        "uq_ci_call_speaker_external_org",
        (
            "organisation_id",
            "integration_id",
            "call_id",
            "external_participant_id",
        ),
        None,
        False,
        None,
    ),
    (
        "ci_calls",
        "constraint",
        "uq_ci_call_external",
        "uq_ci_call_external_org",
        ("organisation_id", "integration_id", "external_id"),
        None,
        False,
        None,
    ),
    (
        "ci_transcript_segments",
        "constraint",
        "uq_ci_transcript_sequence",
        "uq_ci_transcript_sequence_org",
        ("organisation_id", "integration_id", "call_id", "segment_index"),
        None,
        False,
        None,
    ),
    (
        "deal_assets",
        "constraint",
        "uq_deal_asset_source_dedup",
        "uq_deal_asset_source_dedup_org",
        (
            "organisation_id",
            "deal_id",
            "source_type",
            "source_record_id",
            "filename",
        ),
        None,
        False,
        None,
    ),
    (
        "deal_email_associations",
        "constraint",
        "uq_deal_email",
        "uq_deal_email_org",
        ("organisation_id", "deal_id", "internet_message_id"),
        None,
        False,
        None,
    ),
    (
        "deal_timeline_events",
        "constraint",
        "uq_timeline_event_source",
        "uq_timeline_event_source_org",
        ("organisation_id", "deal_id", "event_type", "source_record_id"),
        None,
        False,
        None,
    ),
    (
        "email_folders",
        "constraint",
        "uq_email_folder_external",
        "uq_email_folder_external_org",
        ("organisation_id", "integration_id", "external_folder_id"),
        None,
        False,
        None,
    ),
    (
        "emails",
        "constraint",
        "uq_email_external",
        "uq_email_external_org",
        ("organisation_id", "integration_id", "external_message_id"),
        None,
        False,
        None,
    ),
    (
        "integration_participant_identities",
        "constraint",
        "uq_integration_participant_identity_provider",
        "uq_integration_participant_identity_provider_org",
        ("organisation_id", "integration_id", "provider_participant_id"),
        None,
        False,
        None,
    ),
    (
        "integration_sync_states",
        "index",
        "ux_integration_sync_states_integration_capability",
        "ux_integration_sync_states_integration_capability_org",
        ("organisation_id", "integration_id", "capability"),
        None,
        False,
        None,
    ),
    (
        "knowledge_base",
        "constraint",
        "uq_kb_org_content_hash",
        "uq_kb_org_content_hash_scoped",
        ("organisation_id", "content_hash"),
        None,
        True,
        "DEFERRED",
    ),
    (
        "meeting_recording_sessions",
        "constraint",
        "uq_meeting_recording_dedupe_key",
        "uq_meeting_recording_dedupe_key_org",
        ("organisation_id", "dedupe_key"),
        None,
        False,
        None,
    ),
    (
        "meeting_recording_sessions",
        "constraint",
        "uq_meeting_recording_provider_session",
        "uq_meeting_recording_provider_session_org",
        ("organisation_id", "provider_session_id"),
        None,
        False,
        None,
    ),
    (
        "meetings",
        "constraint",
        "uq_meeting_calendar_event",
        "uq_meeting_calendar_event_org",
        ("organisation_id", "calendar_event_id"),
        None,
        False,
        None,
    ),
    (
        "messaging_channel_integration_states",
        "constraint",
        "uq_messaging_channel_integration_state",
        "uq_messaging_channel_integration_state_org",
        ("organisation_id", "channel_id", "integration_id"),
        None,
        False,
        None,
    ),
    (
        "messaging_channel_members",
        "constraint",
        "uq_channel_member",
        "uq_channel_member_org",
        ("organisation_id", "channel_id", "external_user_id"),
        None,
        False,
        None,
    ),
    (
        "messaging_messages",
        "constraint",
        "uq_messaging_message_external",
        "uq_messaging_message_external_org",
        ("organisation_id", "channel_id", "external_message_id"),
        None,
        False,
        None,
    ),
)


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _qualified(schema_name: str, object_name: str) -> str:
    return f"{_quote_identifier(schema_name)}.{_quote_identifier(object_name)}"


def _migration_schema() -> str:
    """Return the validated shared-schema target."""

    x_args = context.get_x_argument(as_dictionary=True)
    schema_name = x_args.get("schema")
    if schema_name is None:
        raise ValueError("schema is required for organisation migrations")
    return validate_migration_schema_name(schema_name)


def _constraint_exists(schema_name: str, table_name: str, name: str) -> bool:
    return bool(
        op.get_bind()
        .execute(
            sa.text(
                """
                SELECT 1
                  FROM pg_constraint constraint_record
                  JOIN pg_class table_record
                    ON table_record.oid = constraint_record.conrelid
                  JOIN pg_namespace namespace_record
                    ON namespace_record.oid = table_record.relnamespace
                 WHERE namespace_record.nspname = :schema_name
                   AND table_record.relname = :table_name
                   AND constraint_record.conname = :constraint_name
                """
            ),
            {
                "schema_name": schema_name,
                "table_name": table_name,
                "constraint_name": name,
            },
        )
        .scalar_one_or_none()
    )


def _table_exists(schema_name: str, table_name: str) -> bool:
    return bool(
        op.get_bind()
        .execute(
            sa.text(
                """
                SELECT 1
                  FROM pg_class table_record
                  JOIN pg_namespace namespace_record
                    ON namespace_record.oid = table_record.relnamespace
                 WHERE namespace_record.nspname = :schema_name
                   AND table_record.relname = :table_name
                   AND table_record.relkind IN ('r', 'p')
                """
            ),
            {"schema_name": schema_name, "table_name": table_name},
        )
        .scalar_one_or_none()
    )


def _base_columns() -> tuple[sa.Column[object], ...]:
    return (
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("integration_id", sa.Uuid(), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
    )


def _create_tenant_indexes(
    schema_name: str,
    table_name: str,
    columns: Sequence[str],
) -> None:
    for column in columns:
        op.create_index(
            f"ix_{table_name}_org_{column}",
            table_name,
            ["organisation_id", column],
            schema=schema_name,
        )


def _create_conversation_intelligence_tables(schema_name: str) -> None:
    """Create the five classified CI tables omitted by the legacy chain."""

    if not _table_exists(schema_name, "ci_calls"):
        op.create_table(
            "ci_calls",
            *_base_columns(),
            sa.Column("call_id", sa.Uuid(), nullable=False),
            sa.Column("external_id", sa.String(255), nullable=False),
            sa.Column("title", sa.String(), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column(
                "call_medium",
                sa.String(32),
                server_default=sa.text("'meeting'"),
                nullable=False,
            ),
            sa.Column(
                "recording_status",
                sa.String(32),
                server_default=sa.text("'processing'"),
                nullable=False,
            ),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("duration_seconds", sa.Integer(), nullable=True),
            sa.Column("timezone", sa.String(), nullable=True),
            sa.Column("meeting_url", sa.String(), nullable=True),
            sa.Column("recording_url", sa.String(), nullable=True),
            sa.Column("transcript_language", sa.String(), nullable=True),
            sa.Column("owner_user_external_id", sa.String(255), nullable=True),
            sa.Column("crm_owner_id", sa.Uuid(), nullable=True),
            sa.Column("primary_deal_id", sa.Uuid(), nullable=True),
            sa.Column("primary_deal_external_id", sa.String(255), nullable=True),
            sa.Column("source_created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("source_modified_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "call_metadata",
                postgresql.JSONB(),
                server_default=sa.text("'{}'::jsonb"),
                nullable=False,
            ),
            sa.Column("data_payload", postgresql.JSONB(), nullable=True),
            sa.PrimaryKeyConstraint("call_id"),
            schema=schema_name,
        )
        _create_tenant_indexes(
            schema_name,
            "ci_calls",
            ("integration_id", "crm_owner_id", "primary_deal_id", "started_at"),
        )

    if not _table_exists(schema_name, "ci_call_deal_links"):
        op.create_table(
            "ci_call_deal_links",
            *_base_columns(),
            sa.Column("link_id", sa.Uuid(), nullable=False),
            sa.Column("call_id", sa.Uuid(), nullable=False),
            sa.Column("deal_id", sa.Uuid(), nullable=False),
            sa.Column("deal_external_id", sa.String(255), nullable=True),
            sa.Column("association_type", sa.String(64), nullable=True),
            sa.Column("confidence_score", sa.Float(), nullable=True),
            sa.Column("linked_reason", sa.Text(), nullable=True),
            sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("source_modified_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "extra",
                postgresql.JSONB(),
                server_default=sa.text("'{}'::jsonb"),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("link_id"),
            schema=schema_name,
        )
        _create_tenant_indexes(
            schema_name,
            "ci_call_deal_links",
            ("integration_id", "call_id", "deal_id"),
        )

    if not _table_exists(schema_name, "ci_call_speakers"):
        op.create_table(
            "ci_call_speakers",
            *_base_columns(),
            sa.Column("speaker_id", sa.Uuid(), nullable=False),
            sa.Column("call_id", sa.Uuid(), nullable=False),
            sa.Column("external_participant_id", sa.String(255), nullable=True),
            sa.Column(
                "speaker_role",
                sa.String(32),
                server_default=sa.text("'unknown'"),
                nullable=False,
            ),
            sa.Column("speaker_name", sa.String(), nullable=True),
            sa.Column("speaker_email", sa.String(320), nullable=True),
            sa.Column("participant_id", sa.Uuid(), nullable=True),
            sa.Column("crm_contact_id", sa.Uuid(), nullable=True),
            sa.Column("crm_user_id", sa.Uuid(), nullable=True),
            sa.Column("talk_time_seconds", sa.Integer(), nullable=True),
            sa.Column("talk_ratio", sa.Numeric(5, 2), nullable=True),
            sa.Column("sentiment_score", sa.Numeric(5, 2), nullable=True),
            sa.Column(
                "speaker_metadata",
                postgresql.JSONB(),
                server_default=sa.text("'{}'::jsonb"),
                nullable=False,
            ),
            sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("source_modified_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("speaker_id"),
            schema=schema_name,
        )
        _create_tenant_indexes(
            schema_name,
            "ci_call_speakers",
            (
                "integration_id",
                "call_id",
                "participant_id",
                "crm_contact_id",
                "crm_user_id",
            ),
        )

    if not _table_exists(schema_name, "ci_transcript_segments"):
        op.create_table(
            "ci_transcript_segments",
            *_base_columns(),
            sa.Column("segment_id", sa.Uuid(), nullable=False),
            sa.Column("call_id", sa.Uuid(), nullable=False),
            sa.Column("speaker_id", sa.Uuid(), nullable=True),
            sa.Column("speaker_external_id", sa.String(255), nullable=True),
            sa.Column("segment_index", sa.Integer(), nullable=False),
            sa.Column("start_time_seconds", sa.Float(), nullable=True),
            sa.Column("end_time_seconds", sa.Float(), nullable=True),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column(
                "content_format",
                sa.String(32),
                server_default=sa.text("'text'"),
                nullable=False,
            ),
            sa.Column("language_code", sa.String(), nullable=True),
            sa.Column("sentiment_score", sa.Numeric(5, 2), nullable=True),
            sa.Column(
                "keywords",
                postgresql.JSONB(),
                server_default=sa.text("'[]'::jsonb"),
                nullable=False,
            ),
            sa.Column("data_payload", postgresql.JSONB(), nullable=True),
            sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("source_modified_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("segment_id"),
            schema=schema_name,
        )
        _create_tenant_indexes(
            schema_name,
            "ci_transcript_segments",
            ("integration_id", "call_id", "speaker_id"),
        )

    if not _table_exists(schema_name, "ci_call_analytics"):
        op.create_table(
            "ci_call_analytics",
            *_base_columns(),
            sa.Column("analytics_id", sa.Uuid(), nullable=False),
            sa.Column("call_id", sa.Uuid(), nullable=False),
            sa.Column(
                "analysis_type",
                sa.String(64),
                server_default=sa.text("'summary'"),
                nullable=False,
            ),
            sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column(
                "highlights",
                postgresql.JSONB(),
                server_default=sa.text("'[]'::jsonb"),
                nullable=False,
            ),
            sa.Column(
                "action_items",
                postgresql.JSONB(),
                server_default=sa.text("'[]'::jsonb"),
                nullable=False,
            ),
            sa.Column(
                "topics",
                postgresql.JSONB(),
                server_default=sa.text("'[]'::jsonb"),
                nullable=False,
            ),
            sa.Column("overall_sentiment", sa.Numeric(5, 2), nullable=True),
            sa.Column("talk_time_ratio", sa.Numeric(5, 2), nullable=True),
            sa.Column(
                "analytics_payload",
                postgresql.JSONB(),
                server_default=sa.text("'{}'::jsonb"),
                nullable=False,
            ),
            sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("source_modified_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("analytics_id"),
            schema=schema_name,
        )
        _create_tenant_indexes(
            schema_name,
            "ci_call_analytics",
            ("integration_id", "call_id", "generated_at"),
        )


def _index_validity(schema_name: str, index_name: str) -> bool | None:
    return (
        op.get_bind()
        .execute(
            sa.text(
                """
                SELECT index_record.indisvalid
                  FROM pg_index index_record
                  JOIN pg_class class_record
                    ON class_record.oid = index_record.indexrelid
                  JOIN pg_namespace namespace_record
                    ON namespace_record.oid = class_record.relnamespace
                 WHERE namespace_record.nspname = :schema_name
                   AND class_record.relname = :index_name
                """
            ),
            {"schema_name": schema_name, "index_name": index_name},
        )
        .scalar_one_or_none()
    )


def _ensure_concurrent_index(
    schema_name: str,
    table_name: str,
    index_name: str,
    columns: Sequence[str],
    *,
    unique: bool = False,
    where: str | None = None,
) -> None:
    """Build one deterministic index without holding a full-table write lock."""

    validity = _index_validity(schema_name, index_name)
    if validity is True:
        return

    # Failed concurrent builds leave invalid indexes. Only the deterministic
    # Task 5 object is removed before retrying the same definition.
    with op.get_context().autocommit_block():
        if validity is False:
            op.drop_index(
                index_name,
                schema=schema_name,
                postgresql_concurrently=True,
            )
        op.create_index(
            index_name,
            table_name,
            list(columns),
            schema=schema_name,
            unique=unique,
            postgresql_concurrently=True,
            postgresql_where=sa.text(where) if where else None,
        )


def _drop_index_concurrently(schema_name: str, index_name: str) -> None:
    if _index_validity(schema_name, index_name) is None:
        return
    with op.get_context().autocommit_block():
        op.drop_index(
            index_name,
            schema=schema_name,
            postgresql_concurrently=True,
        )


def _candidate_name(table_name: str, identity_column: str) -> str:
    return f"uq_{table_name}_org_{identity_column}"


def _foreign_key_name(table_name: str, local_column: str) -> str:
    return f"fk_{table_name}_{local_column}_org"


def _foreign_key_index_name(table_name: str, local_column: str) -> str:
    return f"ix_{table_name}_org_{local_column}"


def _attach_unique_constraint(
    schema_name: str,
    table_name: str,
    name: str,
    *,
    deferrable: bool = False,
    initially: str | None = None,
) -> None:
    suffix = ""
    if deferrable:
        suffix = " DEFERRABLE"
        if initially is not None:
            suffix += f" INITIALLY {initially}"
    op.execute(
        f"ALTER TABLE {_qualified(schema_name, table_name)} "  # noqa: S608
        f"ADD CONSTRAINT {_quote_identifier(name)} "
        f"UNIQUE USING INDEX {_quote_identifier(name)}{suffix}"
    )


def _legacy_foreign_key_name(
    schema_name: str,
    child_table: str,
    local_column: str,
    parent_table: str,
    remote_column: str,
) -> str | None:
    """Discover the real legacy name rather than assuming naming conventions."""

    return (
        op.get_bind()
        .execute(
            sa.text(
                """
                SELECT constraint_record.conname
                  FROM pg_constraint constraint_record
                  JOIN pg_class child_record
                    ON child_record.oid = constraint_record.conrelid
                  JOIN pg_namespace child_namespace
                    ON child_namespace.oid = child_record.relnamespace
                  JOIN pg_class parent_record
                    ON parent_record.oid = constraint_record.confrelid
                  JOIN pg_namespace parent_namespace
                    ON parent_namespace.oid = parent_record.relnamespace
                  JOIN pg_attribute child_column
                    ON child_column.attrelid = child_record.oid
                   AND child_column.attnum = constraint_record.conkey[1]
                  JOIN pg_attribute parent_column
                    ON parent_column.attrelid = parent_record.oid
                   AND parent_column.attnum = constraint_record.confkey[1]
                 WHERE constraint_record.contype = 'f'
                   AND cardinality(constraint_record.conkey) = 1
                   AND child_namespace.nspname = :schema_name
                   AND parent_namespace.nspname = :schema_name
                   AND child_record.relname = :child_table
                   AND child_column.attname = :local_column
                   AND parent_record.relname = :parent_table
                   AND parent_column.attname = :remote_column
                """
            ),
            {
                "schema_name": schema_name,
                "child_table": child_table,
                "local_column": local_column,
                "parent_table": parent_table,
                "remote_column": remote_column,
            },
        )
        .scalar_one_or_none()
    )


def _drop_legacy_foreign_key(
    schema_name: str,
    child_table: str,
    local_column: str,
    parent_table: str,
    remote_column: str,
) -> None:
    legacy_name = _legacy_foreign_key_name(
        schema_name,
        child_table,
        local_column,
        parent_table,
        remote_column,
    )
    if legacy_name is not None:
        op.drop_constraint(
            legacy_name,
            child_table,
            schema=schema_name,
            type_="foreignkey",
        )


def _drop_legacy_unique_rule(
    schema_name: str,
    table_name: str,
    kind: str,
    legacy_name: str,
) -> None:
    if kind == "constraint":
        if _constraint_exists(schema_name, table_name, legacy_name):
            op.drop_constraint(
                legacy_name,
                table_name,
                schema=schema_name,
                type_="unique",
            )
        return
    _drop_index_concurrently(schema_name, legacy_name)


def _validate_constraint(
    schema_name: str,
    table_name: str,
    constraint_name: str,
) -> None:
    # Identifiers come only from the validated schema and fixed inventories.
    op.execute(
        f"ALTER TABLE {_qualified(schema_name, table_name)} "  # noqa: S608
        f"VALIDATE CONSTRAINT {_quote_identifier(constraint_name)}"
    )


def upgrade() -> None:
    """Install all remaining tenant-qualified integrity rules online."""

    schema_name = _migration_schema()
    _create_conversation_intelligence_tables(schema_name)

    for table_name, identity_column in REMAINING_CANDIDATE_KEYS:
        name = _candidate_name(table_name, identity_column)
        if _constraint_exists(schema_name, table_name, name):
            continue
        _ensure_concurrent_index(
            schema_name,
            table_name,
            name,
            ("organisation_id", identity_column),
            unique=True,
        )
        _attach_unique_constraint(schema_name, table_name, name)

    for table_name, local_column in REMAINING_FOREIGN_KEY_INDEXES:
        _ensure_concurrent_index(
            schema_name,
            table_name,
            _foreign_key_index_name(table_name, local_column),
            ("organisation_id", local_column),
        )

    for (
        child_table,
        local_column,
        parent_table,
        remote_column,
        ondelete,
    ) in REMAINING_TENANT_FOREIGN_KEYS:
        name = _foreign_key_name(child_table, local_column)
        if not _constraint_exists(schema_name, child_table, name):
            op.create_foreign_key(
                name,
                child_table,
                parent_table,
                ["organisation_id", local_column],
                ["organisation_id", remote_column],
                source_schema=schema_name,
                referent_schema=schema_name,
                ondelete=ondelete,
                postgresql_not_valid=True,
            )
            _validate_constraint(schema_name, child_table, name)
        _drop_legacy_foreign_key(
            schema_name,
            child_table,
            local_column,
            parent_table,
            remote_column,
        )

    # Each tenant-scoped rule is live before its stronger global predecessor is
    # removed, so concurrent writes never pass through an unprotected window.
    for (
        table_name,
        kind,
        legacy_name,
        scoped_name,
        columns,
        predicate,
        deferrable,
        initially,
    ) in REMAINING_SCOPED_UNIQUE_RULES:
        if kind == "constraint":
            if not _constraint_exists(schema_name, table_name, scoped_name):
                _ensure_concurrent_index(
                    schema_name,
                    table_name,
                    scoped_name,
                    columns,
                    unique=True,
                )
                _attach_unique_constraint(
                    schema_name,
                    table_name,
                    scoped_name,
                    deferrable=deferrable,
                    initially=initially,
                )
        else:
            _ensure_concurrent_index(
                schema_name,
                table_name,
                scoped_name,
                columns,
                unique=True,
                where=predicate,
            )
        _drop_legacy_unique_rule(
            schema_name,
            table_name,
            kind,
            legacy_name,
        )


def downgrade() -> None:
    """Restore every legacy rule before removing its tenant-scoped replacement."""

    schema_name = _migration_schema()

    for (
        table_name,
        kind,
        legacy_name,
        scoped_name,
        scoped_columns,
        predicate,
        deferrable,
        initially,
    ) in reversed(REMAINING_SCOPED_UNIQUE_RULES):
        legacy_columns = tuple(column for column in scoped_columns if column != "organisation_id")
        if kind == "constraint":
            if not _constraint_exists(schema_name, table_name, legacy_name):
                _ensure_concurrent_index(
                    schema_name,
                    table_name,
                    legacy_name,
                    legacy_columns,
                    unique=True,
                )
                _attach_unique_constraint(
                    schema_name,
                    table_name,
                    legacy_name,
                    deferrable=deferrable,
                    initially=initially,
                )
            if _constraint_exists(schema_name, table_name, scoped_name):
                op.drop_constraint(
                    scoped_name,
                    table_name,
                    schema=schema_name,
                    type_="unique",
                )
        else:
            _ensure_concurrent_index(
                schema_name,
                table_name,
                legacy_name,
                legacy_columns,
                unique=True,
                where=predicate,
            )
            _drop_index_concurrently(schema_name, scoped_name)

    for (
        child_table,
        local_column,
        parent_table,
        remote_column,
        ondelete,
    ) in reversed(REMAINING_TENANT_FOREIGN_KEYS):
        legacy_name = f"{child_table}_{local_column}_fkey"
        if not _constraint_exists(schema_name, child_table, legacy_name):
            op.create_foreign_key(
                legacy_name,
                child_table,
                parent_table,
                [local_column],
                [remote_column],
                source_schema=schema_name,
                referent_schema=schema_name,
                ondelete=ondelete,
                postgresql_not_valid=True,
            )
            _validate_constraint(schema_name, child_table, legacy_name)
        composite_name = _foreign_key_name(child_table, local_column)
        if _constraint_exists(schema_name, child_table, composite_name):
            op.drop_constraint(
                composite_name,
                child_table,
                schema=schema_name,
                type_="foreignkey",
            )

    for table_name, local_column in reversed(REMAINING_FOREIGN_KEY_INDEXES):
        _drop_index_concurrently(
            schema_name,
            _foreign_key_index_name(table_name, local_column),
        )

    for table_name, identity_column in reversed(REMAINING_CANDIDATE_KEYS):
        name = _candidate_name(table_name, identity_column)
        if _constraint_exists(schema_name, table_name, name):
            op.drop_constraint(
                name,
                table_name,
                schema=schema_name,
                type_="unique",
            )
