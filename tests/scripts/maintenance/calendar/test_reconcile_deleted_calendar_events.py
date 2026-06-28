from datetime import UTC, datetime
from uuid import uuid4

from scripts.maintenance.calendar.reconcile_deleted_calendar_events import (
    LocalCalendarState,
    build_provider_snapshot,
    find_missing_local_states,
    format_candidate,
)


def test_provider_snapshot_keeps_active_user_and_canonical_keys() -> None:
    events = [
        {
            "id": "google-user-event-1",
            "iCalUID": "shared-uid-1",
            "status": "confirmed",
            "summary": "Active event",
            "start": {"dateTime": "2026-07-01T10:00:00Z"},
            "end": {"dateTime": "2026-07-01T10:30:00Z"},
        },
        {
            "id": "google-user-event-2",
            "recurringEventId": "series-1",
            "originalStartTime": {"dateTime": "2026-07-08T10:00:00Z"},
            "status": "cancelled",
        },
    ]

    snapshot = build_provider_snapshot(events, "google")

    assert "google-user-event-1" in snapshot.user_external_ids
    assert ("shared-uid-1", "") in snapshot.canonical_keys
    assert "google-user-event-2" not in snapshot.user_external_ids


def test_find_missing_local_states_uses_user_id_then_canonical_key() -> None:
    integration_id = uuid4()
    provider_events = [
        {
            "id": "new-user-copy",
            "iCalUID": "shared-uid-1",
            "status": "confirmed",
            "summary": "Same meeting under changed mailbox id",
            "start": {"dateTime": "2026-07-01T10:00:00Z"},
            "end": {"dateTime": "2026-07-01T10:30:00Z"},
        },
    ]
    snapshot = build_provider_snapshot(provider_events, "google")
    local_rows = [
        LocalCalendarState(
            event_id=uuid4(),
            state_id=uuid4(),
            integration_id=integration_id,
            user_external_id="old-user-copy",
            series_external_id=None,
            external_id="shared-uid-1",
            occurrence_anchor="",
            title="Keep by canonical",
            start_at=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
            status="confirmed",
        ),
        LocalCalendarState(
            event_id=uuid4(),
            state_id=uuid4(),
            integration_id=integration_id,
            user_external_id="missing-user-copy",
            series_external_id=None,
            external_id="shared-uid-2",
            occurrence_anchor="",
            title="Missing",
            start_at=datetime(2026, 7, 2, 10, 0, tzinfo=UTC),
            status="confirmed",
        ),
    ]

    missing = find_missing_local_states(local_rows, snapshot)

    assert [row.user_external_id for row in missing] == ["missing-user-copy"]


def test_find_missing_local_states_keeps_recurring_occurrence_by_series_anchor() -> None:
    integration_id = uuid4()
    provider_events = [
        {
            "id": "outlook-occurrence-1",
            "iCalUId": "occurrence-uid-1",
            "seriesMasterId": "series-master-1",
            "type": "occurrence",
            "subject": "Weekly sync",
            "start": {"dateTime": "2026-07-01T10:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2026-07-01T10:30:00Z", "timeZone": "UTC"},
        },
    ]
    snapshot = build_provider_snapshot(provider_events, "outlook")
    local_rows = [
        LocalCalendarState(
            event_id=uuid4(),
            state_id=uuid4(),
            integration_id=integration_id,
            user_external_id="old-outlook-occurrence-id",
            series_external_id="series-master-1",
            external_id="different-hydrated-uid",
            occurrence_anchor="",
            title="Weekly sync",
            start_at=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
            status="confirmed",
        )
    ]

    missing = find_missing_local_states(local_rows, snapshot)

    assert missing == []


def test_format_candidate_is_json_serializable_operator_summary() -> None:
    candidate = LocalCalendarState(
        event_id=uuid4(),
        state_id=uuid4(),
        integration_id=uuid4(),
        user_external_id="evt-1",
        series_external_id="series-1",
        external_id="uid-1",
        occurrence_anchor="2026-07-01T10:00:00Z",
        title="Deleted meeting",
        start_at=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
        status="confirmed",
    )

    formatted = format_candidate(candidate)

    assert formatted["user_external_id"] == "evt-1"
    assert formatted["series_external_id"] == "series-1"
    assert formatted["start_at"] == "2026-07-01T10:00:00+00:00"
