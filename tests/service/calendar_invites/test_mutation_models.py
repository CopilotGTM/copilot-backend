"""Validation contracts for complete-state calendar meeting updates."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from src.service.calendar_invites.microsoft_graph_client import _outlook_update_body
from src.service.calendar_invites.models import (
    CalendarInviteAttendee,
    CalendarMeetingUpdateRequest,
)


def _request(**overrides) -> CalendarMeetingUpdateRequest:
    values = {
        "organisation_id": "11111111-1111-1111-1111-111111111111",
        "requested_by_user_id": "22222222-2222-2222-2222-222222222222",
        "event_id": "33333333-3333-3333-3333-333333333333",
        "title": "Updated discovery call",
        "start_at": datetime(2026, 8, 3, 9, 0, tzinfo=UTC),
        "end_at": datetime(2026, 8, 3, 9, 30, tzinfo=UTC),
        "timezone": "UTC",
        "attendees": [CalendarInviteAttendee(email="buyer@example.com")],
    }
    values.update(overrides)
    return CalendarMeetingUpdateRequest(**values)


def test_meeting_update_normalizes_times_and_attendees() -> None:
    """Complete-state updates retain aware times and normalized addresses."""
    request = _request()

    assert request.start_at.tzinfo is UTC
    assert request.attendees[0].email == "buyer@example.com"


def test_meeting_update_rejects_non_positive_duration() -> None:
    """Invalid meeting windows fail before any provider mutation."""
    with pytest.raises(ValidationError, match="end_at must be after start_at"):
        _request(end_at=datetime(2026, 8, 3, 9, 0, tzinfo=UTC))


def test_outlook_notes_update_preserves_teams_join_block() -> None:
    """Changing notes must not remove Graph's embedded Teams controls."""
    request = _request(
        body="Updated agenda",
        body_changed=True,
        previous_body="Original agenda",
        provider_body="<p>Original agenda</p><div>Join Microsoft Teams Meeting</div>",
        provider_body_content_type="HTML",
    )

    body = _outlook_update_body(request)

    assert body == {
        "contentType": "HTML",
        "content": "<p>Updated agenda</p><div>Join Microsoft Teams Meeting</div>",
    }


def test_outlook_unchanged_notes_are_omitted_from_patch() -> None:
    """Unchanged notes are omitted so Graph retains its body verbatim."""
    assert _outlook_update_body(_request(body_changed=False)) is None
