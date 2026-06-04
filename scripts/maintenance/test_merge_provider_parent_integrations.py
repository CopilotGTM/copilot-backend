from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from scripts.maintenance.merge_provider_parent_integrations import (
    EVENT_CENTRIC_TABLES,
    MASTER_REWRITE_TABLES,
    ORG_REWRITE_TABLES,
    IntegrationRecord,
    IntegrationReference,
    RemainingReferenceError,
    build_merge_plans,
    build_sync_state_payload,
    ensure_no_remaining_references,
)

ORG_ID = UUID("11111111-1111-1111-1111-111111111111")
USER_ID = UUID("22222222-2222-2222-2222-222222222222")
GMAIL_ID = UUID("33333333-3333-3333-3333-333333333333")
GOOGLE_CALENDAR_ID = UUID("44444444-4444-4444-4444-444444444444")
OUTLOOK_EMAIL_ID = UUID("55555555-5555-5555-5555-555555555555")
OUTLOOK_CALENDAR_ID = UUID("66666666-6666-6666-6666-666666666666")
TEAMS_ID = UUID("77777777-7777-7777-7777-777777777777")


def _record(
    integration_id: UUID,
    integration_type: str,
    *,
    metadata: dict | None = None,
    updated_at: datetime | None = None,
) -> IntegrationRecord:
    return IntegrationRecord(
        integration_id=integration_id,
        organisation_id=ORG_ID,
        created_by_user_id=USER_ID,
        integration_email="OWNER@Example.com",
        connection_id="connected-account",
        integration_type=integration_type,
        name=integration_type,
        is_active=True,
        last_synced_at=None,
        last_sync_status=None,
        last_sync_error=None,
        consecutive_sync_failures=0,
        connection_metadata=metadata or {},
        updated_at=updated_at or datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_google_merge_plan_chooses_calendar_and_rewrites_email_references() -> None:
    plans = build_merge_plans(
        [
            _record(GMAIL_ID, "gmail"),
            _record(GOOGLE_CALENDAR_ID, "google_calendar"),
        ],
        provider="google",
    )

    assert len(plans) == 1
    plan = plans[0]
    assert plan.survivor.integration_id == GOOGLE_CALENDAR_ID
    assert plan.retired_ids == [GMAIL_ID]
    assert ORG_REWRITE_TABLES["emails"].column_name == "integration_id"
    assert ORG_REWRITE_TABLES["email_folders"].column_name == "integration_id"


def test_single_legacy_child_is_promoted_to_provider_parent() -> None:
    plans = build_merge_plans([_record(GMAIL_ID, "gmail")], provider="google")

    assert len(plans) == 1
    assert plans[0].survivor.integration_id == GMAIL_ID
    assert plans[0].retired_ids == []


def test_microsoft_merge_plan_chooses_calendar_before_email_and_teams() -> None:
    plans = build_merge_plans(
        [
            _record(OUTLOOK_EMAIL_ID, "outlook_email"),
            _record(TEAMS_ID, "teams"),
            _record(OUTLOOK_CALENDAR_ID, "outlook_calendar"),
        ],
        provider="microsoft",
    )

    assert len(plans) == 1
    plan = plans[0]
    assert plan.survivor.integration_id == OUTLOOK_CALENDAR_ID
    assert plan.retired_ids == [OUTLOOK_EMAIL_ID, TEAMS_ID]


def test_reference_coverage_rewrites_user_state_and_participants_but_not_event_tables() -> None:
    assert "calendar_event_user_states" in ORG_REWRITE_TABLES
    assert "integration_participants" in ORG_REWRITE_TABLES
    assert "integration_participant_identities" in ORG_REWRITE_TABLES
    assert "calendar_events" in EVENT_CENTRIC_TABLES
    assert "calendar_event_attendees" in EVENT_CENTRIC_TABLES
    assert "calendar_event_deal_associations" in EVENT_CENTRIC_TABLES
    assert "calendar_events" not in ORG_REWRITE_TABLES


def test_master_organisation_integration_details_rewrites_to_survivor() -> None:
    assert MASTER_REWRITE_TABLES["organisation_integration_details"].schema == "public"
    assert MASTER_REWRITE_TABLES["organisation_integration_details"].column_name == (
        "integration_id"
    )


def test_sync_state_payload_moves_cursor_metadata_and_stales_webhook_metadata() -> None:
    row = _record(
        GMAIL_ID,
        "gmail",
        metadata={
            "gmail_history_id": "history-1",
            "google_webhook_subscriptions": {"gmail": {"watch_history_id": "old-watch"}},
            "unrelated": "parent-data",
        },
    )

    payload = build_sync_state_payload(row, survivor_id=GOOGLE_CALENDAR_ID)

    assert payload["integration_id"] == GOOGLE_CALENDAR_ID
    assert payload["capability"] == "gmail"
    assert payload["cursor_metadata"] == {"gmail_history_id": "history-1"}
    assert payload["webhook_metadata"] == {}
    assert payload["sync_metadata"]["provider_parent_migration"]["webhook_metadata_stale"] is True
    assert payload["sync_metadata"]["provider_parent_migration"]["legacy_integration_id"] == str(
        GMAIL_ID
    )


def test_unknown_remaining_reference_blocks_hard_delete() -> None:
    with pytest.raises(RemainingReferenceError) as exc_info:
        ensure_no_remaining_references(
            [
                IntegrationReference(
                    schema="org_111",
                    table_name="unexpected_table",
                    column_name="integration_id",
                    row_count=1,
                )
            ]
        )

    assert "unexpected_table.integration_id" in str(exc_info.value)
