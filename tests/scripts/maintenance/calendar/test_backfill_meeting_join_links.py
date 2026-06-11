from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from scripts.maintenance.calendar.backfill_meeting_join_links import (
    BackfillCandidate,
    extract_join_link_update,
    format_sample,
    queue_recording_dispatches,
    queued_event_ids_for_updated_rows,
)


def test_extract_join_link_update_reads_outlook_location_google_meet() -> None:
    payload = {
        "location": {"displayName": "https://meet.google.com/yqz-yaqx-dup"},
        "locations": [
            {
                "displayName": "https://meet.google.com/yqz-yaqx-dup",
                "uniqueId": "https://meet.google.com/yqz-yaqx-dup",
            }
        ],
    }

    update = extract_join_link_update("outlook", payload)

    assert update == {
        "conference_link": "https://meet.google.com/yqz-yaqx-dup",
        "online_meeting_provider": "googleMeet",
    }


def test_extract_join_link_update_reads_google_description_zoom() -> None:
    payload = {
        "description": "Join Zoom Meeting https://acme.zoom.us/j/987654321",
    }

    update = extract_join_link_update("google", payload)

    assert update == {
        "conference_link": "https://acme.zoom.us/j/987654321",
        "online_meeting_provider": "zoom",
    }


def test_extract_join_link_update_reads_null_provider_google_conference_data() -> None:
    payload = {
        "conferenceData": {
            "entryPoints": [
                {"entryPointType": "video", "uri": "https://meet.google.com/abc-defg-hij"}
            ],
        },
    }

    update = extract_join_link_update(None, payload)

    assert update == {
        "conference_link": "https://meet.google.com/abc-defg-hij",
        "online_meeting_provider": "googleMeet",
    }


def test_extract_join_link_update_reads_null_provider_outlook_location() -> None:
    payload = {
        "location": {"displayName": "https://meet.google.com/yqz-yaqx-dup"},
    }

    update = extract_join_link_update(None, payload)

    assert update == {
        "conference_link": "https://meet.google.com/yqz-yaqx-dup",
        "online_meeting_provider": "googleMeet",
    }


def test_extract_join_link_update_rejects_unknown_url() -> None:
    payload = {
        "location": {"displayName": "https://example.com/join"},
    }

    assert extract_join_link_update("outlook", payload) is None


def test_format_sample_includes_operator_review_fields() -> None:
    candidate = BackfillCandidate(
        event_id=uuid4(),
        title="Meet with Carlos",
        start_at=datetime(2026, 6, 11, 17, 0, tzinfo=UTC),
        provider="outlook",
        conference_link="https://meet.google.com/yqz-yaqx-dup",
        online_meeting_provider="googleMeet",
    )

    sample = format_sample(candidate)

    assert sample["title"] == "Meet with Carlos"
    assert sample["provider"] == "outlook"
    assert sample["conference_host"] == "meet.google.com"
    assert sample["online_meeting_provider"] == "googleMeet"


def test_queued_event_ids_only_returns_updated_rows_inside_dispatch_window() -> None:
    now = datetime(2026, 6, 11, 16, 45, tzinfo=UTC)
    eligible = BackfillCandidate(
        event_id=uuid4(),
        title="Eligible",
        start_at=now + timedelta(minutes=15),
        provider="outlook",
        conference_link="https://meet.google.com/abc-defg-hij",
        online_meeting_provider="googleMeet",
    )
    too_late = BackfillCandidate(
        event_id=uuid4(),
        title="Too late",
        start_at=now - timedelta(minutes=1),
        provider="outlook",
        conference_link="https://meet.google.com/late-link",
        online_meeting_provider="googleMeet",
    )

    assert queued_event_ids_for_updated_rows([eligible, too_late], now) == [eligible.event_id]


@pytest.mark.asyncio
async def test_queue_recording_dispatches_continues_after_one_failure() -> None:
    organisation_id = uuid4()
    first_event_id = uuid4()
    second_event_id = uuid4()
    send_task = AsyncMock(side_effect=[RuntimeError("redis unavailable"), "task-2"])

    result = await queue_recording_dispatches(
        organisation_id,
        [first_event_id, second_event_id],
        send_task=send_task,
    )

    assert result.queued == 1
    assert result.queue_failed == 1
    assert result.failed_event_ids == (str(first_event_id),)
    assert send_task.await_count == 2
