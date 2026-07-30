"""Tests for Microsoft Graph payload helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from src.service.calendar_invites.microsoft_graph_client import (
    MicrosoftCalendarInviteClient,
    build_microsoft_event_payload,
)
from src.service.calendar_invites.models import (
    CalendarInviteAttendee,
    CalendarInviteCreateRequest,
    CalendarInviteError,
    CalendarMeetingUpdateRequest,
    CalendarNotificationScope,
)
from src.service.calendar_invites.timezones import to_outlook_timezone


def _update_request(
    scope: CalendarNotificationScope,
    *,
    changed_fields: frozenset[str] = frozenset({"attendees"}),
) -> CalendarMeetingUpdateRequest:
    """Build one Outlook update with an explicit notification contract."""

    return CalendarMeetingUpdateRequest(
        organisation_id="11111111-1111-1111-1111-111111111111",
        requested_by_user_id="22222222-2222-2222-2222-222222222222",
        event_id="33333333-3333-3333-3333-333333333333",
        expected_version='W/"etag-1"',
        provider_etag='W/"etag-1"',
        title="Updated discovery call",
        start_at=datetime(2026, 5, 12, 14, 30, tzinfo=UTC),
        end_at=datetime(2026, 5, 12, 15, 0, tzinfo=UTC),
        timezone="UTC",
        attendees=[CalendarInviteAttendee(email="buyer@example.com")],
        changed_fields=changed_fields,
        notification_scope=scope,
    )


def test_to_outlook_timezone_maps_iana_to_windows_name() -> None:
    """Timezone conversion should use Graph-compatible Windows identifiers."""
    # Contract: IANA inputs from model validation must convert to Graph Windows names.
    assert to_outlook_timezone("Asia/Kolkata") == "India Standard Time"
    assert to_outlook_timezone("America/Los_Angeles") == "Pacific Standard Time"
    assert to_outlook_timezone("UTC") == "UTC"


def test_build_microsoft_event_payload_creates_calendar_backed_teams_event() -> None:
    """Payload builder should request Teams-backed online meetings for invite sends."""
    # Contract: payload must request onlineMeeting and include idempotency key when present.
    payload = build_microsoft_event_payload(
        CalendarInviteCreateRequest(
            organisation_id="11111111-1111-1111-1111-111111111111",
            requested_by_user_id="22222222-2222-2222-2222-222222222222",
            title="Discovery call",
            body="Discuss rollout plan",
            location="Teams",
            start_at=datetime(2026, 5, 12, 14, 30, tzinfo=UTC),
            end_at=datetime(2026, 5, 12, 15, 0, tzinfo=UTC),
            timezone="Asia/Kolkata",
            attendees=[
                CalendarInviteAttendee(email="buyer@example.com", name="Buyer"),
                CalendarInviteAttendee(email="ae@example.com"),
            ],
            idempotency_key="invite-123",
        )
    )

    assert payload["subject"] == "Discovery call"
    assert payload["isOnlineMeeting"] is True
    assert payload["onlineMeetingProvider"] == "teamsForBusiness"
    assert payload["transactionId"] == "invite-123"
    assert payload["body"] == {"contentType": "Text", "content": "Discuss rollout plan"}
    assert payload["start"] == {
        "dateTime": "2026-05-12T20:00:00",
        "timeZone": "India Standard Time",
    }
    assert payload["end"] == {
        "dateTime": "2026-05-12T20:30:00",
        "timeZone": "India Standard Time",
    }
    assert payload["attendees"] == [
        {
            "emailAddress": {"address": "buyer@example.com", "name": "Buyer"},
            "type": "required",
        },
        {
            "emailAddress": {"address": "ae@example.com", "name": "ae@example.com"},
            "type": "required",
        },
    ]


def test_build_microsoft_event_payload_rejects_missing_attendees() -> None:
    """Provider payload build must fail early when no attendee is supplied."""
    # Contract: invalid request with no attendees must fail at payload build-time.
    with pytest.raises(ValueError, match="attendee"):
        build_microsoft_event_payload(
            CalendarInviteCreateRequest(
                organisation_id="11111111-1111-1111-1111-111111111111",
                requested_by_user_id="22222222-2222-2222-2222-222222222222",
                title="Discovery call",
                start_at=datetime(2026, 5, 12, 14, 30, tzinfo=UTC),
                end_at=datetime(2026, 5, 12, 15, 0, tzinfo=UTC),
                timezone="UTC",
                attendees=[],
            )
        )


def test_calendar_invite_request_rejects_invalid_attendee_email() -> None:
    """Attendee email validation should reject malformed provider targets."""
    with pytest.raises(ValidationError):
        CalendarInviteAttendee(email="not-an-email")


def test_calendar_invite_request_rejects_oversized_attendee_list() -> None:
    """Tool input bounds should reject oversized attendee fanout before Graph."""
    attendees = [CalendarInviteAttendee(email=f"buyer-{index}@example.com") for index in range(101)]

    with pytest.raises(ValidationError):
        CalendarInviteCreateRequest(
            organisation_id="11111111-1111-1111-1111-111111111111",
            requested_by_user_id="22222222-2222-2222-2222-222222222222",
            title="Discovery call",
            start_at=datetime(2026, 5, 12, 14, 30, tzinfo=UTC),
            end_at=datetime(2026, 5, 12, 15, 0, tzinfo=UTC),
            timezone="UTC",
            attendees=attendees,
        )


@pytest.mark.asyncio
async def test_microsoft_client_includes_safe_provider_error_summary(monkeypatch) -> None:
    """Immediate Graph rejections should keep useful provider detail for the agent."""

    async def fake_retry(token_info, request_fn):
        """Simulate an immediate Microsoft Graph validation rejection."""
        request = httpx.Request("POST", "https://graph.microsoft.com/v1.0/me/events")
        response = httpx.Response(
            400,
            request=request,
            json={"error": {"message": "At least one attendee is invalid."}},
        )
        raise httpx.HTTPStatusError("bad request", request=request, response=response)

    monkeypatch.setattr(
        "src.service.calendar_invites.microsoft_graph_client.call_with_token_retry",
        fake_retry,
    )

    with pytest.raises(CalendarInviteError) as exc_info:
        await MicrosoftCalendarInviteClient().create_event(
            SimpleNamespace(access_token="token"),
            CalendarInviteCreateRequest(
                organisation_id="11111111-1111-1111-1111-111111111111",
                requested_by_user_id="22222222-2222-2222-2222-222222222222",
                title="Discovery call",
                start_at=datetime(2026, 5, 12, 14, 30, tzinfo=UTC),
                end_at=datetime(2026, 5, 12, 15, 0, tzinfo=UTC),
                timezone="UTC",
                attendees=[CalendarInviteAttendee(email="buyer@example.com")],
            ),
        )

    assert exc_info.value.code == "calendar_provider_error"
    assert "Outlook Calendar rejected the invite request" in exc_info.value.message
    assert "At least one attendee is invalid" in exc_info.value.message


@pytest.mark.asyncio
async def test_outlook_changed_attendee_scope_sends_attendees_only(monkeypatch) -> None:
    """Graph must receive only attendees to use its selective-update behavior."""

    calls: list[dict[str, object]] = []

    async def fake_retry(token_info, request_fn):
        """Run the PATCH with a stable fake Graph access token."""
        return await request_fn("graph-token")

    async def fake_patch(client, endpoint, *, json_body):
        """Capture the attendee-only Graph update."""
        calls.append({"endpoint": endpoint, "headers": dict(client.headers), "json": json_body})
        return {"id": "event-1", "@odata.etag": 'W/"etag-2"'}

    monkeypatch.setattr(
        "src.service.calendar_invites.microsoft_graph_client.call_with_token_retry",
        fake_retry,
    )
    monkeypatch.setattr(
        "src.service.calendar_invites.microsoft_graph_client.graph_api_patch_json",
        fake_patch,
    )

    await MicrosoftCalendarInviteClient().update_event(
        SimpleNamespace(),
        "event-1",
        _update_request(CalendarNotificationScope.CHANGED_ATTENDEES_ONLY),
    )

    assert set(calls[0]["json"]) == {"attendees"}
    assert calls[0]["headers"]["if-match"] == 'W/"etag-1"'


@pytest.mark.asyncio
async def test_outlook_all_attendee_scope_adds_only_notification_trigger(monkeypatch) -> None:
    """Explicit all-attendee delivery should not rewrite unrelated meeting fields."""

    calls: list[dict[str, object]] = []

    async def fake_retry(token_info, request_fn):
        """Run the PATCH with a stable fake Graph access token."""
        return await request_fn("graph-token")

    async def fake_patch(client, endpoint, *, json_body):
        """Capture the roster plus its all-attendee notification trigger."""
        calls.append(json_body)
        return {"id": "event-1", "@odata.etag": 'W/"etag-2"'}

    monkeypatch.setattr(
        "src.service.calendar_invites.microsoft_graph_client.call_with_token_retry",
        fake_retry,
    )
    monkeypatch.setattr(
        "src.service.calendar_invites.microsoft_graph_client.graph_api_patch_json",
        fake_patch,
    )

    await MicrosoftCalendarInviteClient().update_event(
        SimpleNamespace(),
        "event-1",
        _update_request(CalendarNotificationScope.ALL_ATTENDEES),
    )

    assert set(calls[0]) == {"subject", "attendees"}


@pytest.mark.asyncio
async def test_outlook_subject_update_omits_attendee_roster(monkeypatch) -> None:
    """A scalar Graph update must leave RSVP and resource state untouched."""

    calls: list[dict[str, object]] = []

    async def fake_retry(token_info, request_fn):
        """Run the PATCH with a stable fake Graph access token."""
        return await request_fn("graph-token")

    async def fake_patch(client, endpoint, *, json_body):
        """Capture the sparse subject-only Graph update."""
        calls.append(json_body)
        return {"id": "event-1", "@odata.etag": 'W/"etag-2"'}

    monkeypatch.setattr(
        "src.service.calendar_invites.microsoft_graph_client.call_with_token_retry",
        fake_retry,
    )
    monkeypatch.setattr(
        "src.service.calendar_invites.microsoft_graph_client.graph_api_patch_json",
        fake_patch,
    )

    await MicrosoftCalendarInviteClient().update_event(
        SimpleNamespace(),
        "event-1",
        _update_request(
            CalendarNotificationScope.ALL_ATTENDEES,
            changed_fields=frozenset({"title"}),
        ),
    )

    assert calls[0] == {"subject": "Updated discovery call"}


@pytest.mark.asyncio
async def test_outlook_attendee_update_preserves_room_and_response(monkeypatch) -> None:
    """An attendee-only Graph update must retain unchanged resource semantics."""

    calls: list[dict[str, object]] = []

    async def fake_retry(token_info, request_fn):
        """Run the PATCH with a stable fake Graph access token."""
        return await request_fn("graph-token")

    async def fake_patch(client, endpoint, *, json_body):
        """Capture the metadata-preserving attendee update."""
        calls.append(json_body)
        return {"id": "event-1", "@odata.etag": 'W/"etag-2"'}

    monkeypatch.setattr(
        "src.service.calendar_invites.microsoft_graph_client.call_with_token_retry",
        fake_retry,
    )
    monkeypatch.setattr(
        "src.service.calendar_invites.microsoft_graph_client.graph_api_patch_json",
        fake_patch,
    )
    request = _update_request(CalendarNotificationScope.CHANGED_ATTENDEES_ONLY).model_copy(
        update={
            "attendees": [
                CalendarInviteAttendee(email="room@example.com"),
                CalendarInviteAttendee(email="new@example.com"),
            ],
            "provider_attendees": [
                {
                    "emailAddress": {
                        "address": "room@example.com",
                        "name": "Board Room",
                    },
                    "type": "resource",
                    "status": {"response": "accepted"},
                }
            ],
        }
    )

    await MicrosoftCalendarInviteClient().update_event(
        SimpleNamespace(),
        "event-1",
        request,
    )

    assert calls[0]["attendees"] == [
        {
            "emailAddress": {
                "address": "room@example.com",
                "name": "Board Room",
            },
            "status": {"response": "accepted"},
            "type": "resource",
        },
        {
            "emailAddress": {
                "address": "new@example.com",
                "name": "new@example.com",
            },
            "type": "required",
        },
    ]
