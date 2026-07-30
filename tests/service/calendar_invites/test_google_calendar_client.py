"""Tests for Google Calendar invite payload and client behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from src.service.calendar_invites.google_calendar_client import (
    GoogleCalendarInviteClient,
    build_google_event_payload,
)
from src.service.calendar_invites.models import (
    CalendarInviteAttendee,
    CalendarInviteCreateRequest,
    CalendarInviteError,
    CalendarMeetingUpdateRequest,
    CalendarNotificationScope,
)
from src.service.integrations.api_error_utils import IntegrationPermissionError


def _request() -> CalendarInviteCreateRequest:
    return CalendarInviteCreateRequest(
        organisation_id="11111111-1111-1111-1111-111111111111",
        requested_by_user_id="22222222-2222-2222-2222-222222222222",
        title="Discovery call",
        body="Discuss rollout plan",
        location="Google Meet",
        start_at=datetime(2026, 5, 12, 14, 30, tzinfo=UTC),
        end_at=datetime(2026, 5, 12, 15, 0, tzinfo=UTC),
        timezone="Asia/Kolkata",
        attendees=[CalendarInviteAttendee(email="buyer@example.com", name="Buyer")],
        idempotency_key="invite-123",
    )


def _update_request(
    scope: CalendarNotificationScope = CalendarNotificationScope.ALL_ATTENDEES,
    *,
    changed_fields: frozenset[str] = frozenset({"title"}),
) -> CalendarMeetingUpdateRequest:
    """Build one version-bound Google update for provider-client tests."""

    return CalendarMeetingUpdateRequest(
        organisation_id="11111111-1111-1111-1111-111111111111",
        requested_by_user_id="22222222-2222-2222-2222-222222222222",
        event_id="33333333-3333-3333-3333-333333333333",
        expected_version='"etag-1"',
        provider_etag='"etag-1"',
        title="Updated discovery call",
        start_at=datetime(2026, 5, 12, 14, 30, tzinfo=UTC),
        end_at=datetime(2026, 5, 12, 15, 0, tzinfo=UTC),
        timezone="UTC",
        attendees=[CalendarInviteAttendee(email="buyer@example.com")],
        changed_fields=changed_fields,
        notification_scope=scope,
    )


def test_build_google_event_payload_creates_meet_invite_payload() -> None:
    """Google payload should create a Meet-backed invite with attendees."""
    payload = build_google_event_payload(_request())

    assert payload["summary"] == "Discovery call"
    assert payload["description"] == "Discuss rollout plan"
    assert payload["location"] == "Google Meet"
    assert payload["start"] == {
        "dateTime": "2026-05-12T20:00:00+05:30",
        "timeZone": "Asia/Kolkata",
    }
    assert payload["end"] == {
        "dateTime": "2026-05-12T20:30:00+05:30",
        "timeZone": "Asia/Kolkata",
    }
    assert payload["attendees"] == [{"email": "buyer@example.com", "displayName": "Buyer"}]
    assert payload["conferenceData"]["createRequest"]["requestId"] == "invite-123"


def test_build_google_event_payload_uses_safe_fallback_conference_request_id() -> None:
    """Fallback Meet request IDs should avoid punctuation that providers may reject."""
    request = _request().model_copy(update={"idempotency_key": None})

    payload = build_google_event_payload(request)

    request_id = payload["conferenceData"]["createRequest"]["requestId"]
    assert request_id.startswith("invite-")
    assert request_id.replace("invite-", "").isalnum()


def test_build_google_event_payload_rejects_invalid_timezone() -> None:
    """Invalid user timezones should fail before making an outbound provider call."""
    request = _request().model_copy(update={"timezone": "India Standard Time"})

    with pytest.raises(CalendarInviteError) as exc_info:
        build_google_event_payload(request)

    assert exc_info.value.code == "invalid_calendar_timezone"


@pytest.mark.asyncio
async def test_google_client_posts_insert_with_invite_notifications(monkeypatch) -> None:
    """Client should call Events.insert with sendUpdates=all and conference data."""
    calls: list[dict[str, object]] = []

    async def fake_call_with_token_retry(token_info, request_fn):
        """Run the request with a stable fake Google access token."""
        return await request_fn("google-token")

    async def fake_post_json(client, endpoint, *, json_body, params):
        """Capture the Google insert request and return a provider event."""
        calls.append(
            {
                "endpoint": endpoint,
                "headers": dict(client.headers),
                "json_body": json_body,
                "params": params,
            }
        )
        return {
            "id": "google-event-1",
            "iCalUID": "icaluid-1",
            "summary": json_body["summary"],
        }

    monkeypatch.setattr(
        "src.service.calendar_invites.google_calendar_client.call_with_token_retry",
        fake_call_with_token_retry,
    )
    monkeypatch.setattr(
        "src.service.calendar_invites.google_calendar_client.google_calendar_api_post_json",
        fake_post_json,
    )

    result = await GoogleCalendarInviteClient().create_event(
        SimpleNamespace(access_token="token"),
        _request(),
    )

    assert result["id"] == "google-event-1"
    assert calls[0]["endpoint"] == "/calendar/v3/calendars/primary/events"
    assert calls[0]["params"] == {"sendUpdates": "all", "conferenceDataVersion": 1}
    assert calls[0]["headers"]["authorization"] == "Bearer google-token"


@pytest.mark.asyncio
async def test_google_client_maps_insufficient_scope_to_invite_error(monkeypatch) -> None:
    """Google insufficient-scope errors should ask for Calendar write reconnect."""

    async def fake_call_with_token_retry(token_info, request_fn):
        """Simulate a Google token that lacks Calendar write access."""
        raise IntegrationPermissionError(
            "insufficient scope",
            status_code=403,
            provider="google",
            endpoint="/calendar/v3/calendars/primary/events",
        )

    monkeypatch.setattr(
        "src.service.calendar_invites.google_calendar_client.call_with_token_retry",
        fake_call_with_token_retry,
    )

    with pytest.raises(CalendarInviteError) as exc_info:
        await GoogleCalendarInviteClient().create_event(
            SimpleNamespace(access_token="token"), _request()
        )

    assert exc_info.value.code == "calendar_write_scope_required"


@pytest.mark.asyncio
async def test_google_client_includes_safe_provider_error_summary(monkeypatch) -> None:
    """Immediate Google rejections should keep useful provider detail for the agent."""

    async def fake_call_with_token_retry(token_info, request_fn):
        """Simulate an immediate Google validation rejection."""
        request = httpx.Request(
            "POST",
            "https://www.googleapis.com/calendar/v3/calendars/primary/events",
        )
        response = httpx.Response(
            400,
            request=request,
            json={"error": {"message": "Invalid attendee email."}},
        )
        raise httpx.HTTPStatusError("bad request", request=request, response=response)

    monkeypatch.setattr(
        "src.service.calendar_invites.google_calendar_client.call_with_token_retry",
        fake_call_with_token_retry,
    )

    with pytest.raises(CalendarInviteError) as exc_info:
        await GoogleCalendarInviteClient().create_event(
            SimpleNamespace(access_token="token"), _request()
        )

    assert exc_info.value.code == "calendar_provider_error"
    assert "Google Calendar rejected the invite request" in exc_info.value.message
    assert "Invalid attendee email" in exc_info.value.message


@pytest.mark.asyncio
async def test_google_update_uses_organizer_etag_and_selected_notifications(
    monkeypatch,
) -> None:
    """Google PATCH must bind the organizer ETag and honor the all/none scope."""

    calls: list[dict[str, object]] = []

    async def fake_retry(token_info, request_fn):
        """Run the PATCH with a stable fake Google access token."""
        return await request_fn("google-token")

    class FakeClient:
        """Capture the outbound PATCH without network access."""

        def __init__(self, *, headers, timeout):
            self.headers = headers

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def patch(self, url, *, json, params):
            """Capture the update request and return its new ETag."""
            calls.append({"url": url, "headers": self.headers, "json": json, "params": params})
            request = httpx.Request("PATCH", url)
            return httpx.Response(
                200,
                request=request,
                json={"id": "google-event-1", "etag": '"etag-2"'},
            )

    monkeypatch.setattr(
        "src.service.calendar_invites.google_calendar_client.call_with_token_retry",
        fake_retry,
    )
    monkeypatch.setattr(
        "src.service.calendar_invites.google_calendar_client.httpx.AsyncClient",
        FakeClient,
    )

    await GoogleCalendarInviteClient().update_event(
        SimpleNamespace(),
        "google-event-1",
        _update_request(CalendarNotificationScope.NONE),
    )

    assert calls[0]["headers"]["If-Match"] == '"etag-1"'
    assert calls[0]["params"]["sendUpdates"] == "none"
    assert calls[0]["json"] == {"summary": "Updated discovery call"}


@pytest.mark.asyncio
async def test_google_attendee_update_preserves_existing_guest_state(monkeypatch) -> None:
    """Replacing Google's attendee array must retain unchanged RSVP/resource metadata."""

    calls: list[dict[str, object]] = []

    async def fake_retry(token_info, request_fn):
        """Run the PATCH with a stable fake Google access token."""
        return await request_fn("google-token")

    class FakeClient:
        """Capture the attendee PATCH without network access."""

        def __init__(self, *, headers, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def patch(self, url, *, json, params):
            """Capture the roster and return a provider-style response."""
            calls.append(json)
            request = httpx.Request("PATCH", url)
            return httpx.Response(200, request=request, json={"id": "event-1"})

    monkeypatch.setattr(
        "src.service.calendar_invites.google_calendar_client.call_with_token_retry",
        fake_retry,
    )
    monkeypatch.setattr(
        "src.service.calendar_invites.google_calendar_client.httpx.AsyncClient",
        FakeClient,
    )
    request = _update_request(changed_fields=frozenset({"attendees"})).model_copy(
        update={
            "attendees": [
                CalendarInviteAttendee(email="room@example.com"),
                CalendarInviteAttendee(email="new@example.com"),
            ],
            "provider_attendees": [
                {
                    "email": "room@example.com",
                    "displayName": "Board Room",
                    "resource": True,
                    "responseStatus": "accepted",
                }
            ],
        }
    )

    await GoogleCalendarInviteClient().update_event(
        SimpleNamespace(),
        "event-1",
        request,
    )

    assert calls[0] == {
        "attendees": [
            {
                "email": "room@example.com",
                "displayName": "Board Room",
                "resource": True,
                "responseStatus": "accepted",
            },
            {"email": "new@example.com"},
        ]
    }


@pytest.mark.asyncio
async def test_google_stale_update_maps_to_refreshable_conflict(monkeypatch) -> None:
    """A Google 412 must become the stable meeting-changed contract."""

    async def fake_retry(token_info, request_fn):
        """Simulate Google rejecting an update with a stale ETag."""
        request = httpx.Request(
            "PATCH",
            "https://www.googleapis.com/calendar/v3/calendars/primary/events/event-1",
        )
        response = httpx.Response(412, request=request)
        raise httpx.HTTPStatusError("stale", request=request, response=response)

    monkeypatch.setattr(
        "src.service.calendar_invites.google_calendar_client.call_with_token_retry",
        fake_retry,
    )

    with pytest.raises(CalendarInviteError) as exc_info:
        await GoogleCalendarInviteClient().update_event(
            SimpleNamespace(),
            "event-1",
            _update_request(),
        )

    assert exc_info.value.code == "calendar_meeting_changed"


@pytest.mark.asyncio
async def test_google_stale_cancellation_maps_to_refreshable_conflict(monkeypatch) -> None:
    """Cancellation must use the same organizer-copy concurrency contract."""

    async def fake_retry(token_info, request_fn):
        """Simulate Google rejecting a cancellation with a stale ETag."""
        request = httpx.Request(
            "DELETE",
            "https://www.googleapis.com/calendar/v3/calendars/primary/events/event-1",
        )
        response = httpx.Response(412, request=request)
        raise httpx.HTTPStatusError("stale", request=request, response=response)

    monkeypatch.setattr(
        "src.service.calendar_invites.google_calendar_client.call_with_token_retry",
        fake_retry,
    )

    with pytest.raises(CalendarInviteError) as exc_info:
        await GoogleCalendarInviteClient().cancel_event(
            SimpleNamespace(),
            "event-1",
            '"organizer-etag"',
        )

    assert exc_info.value.code == "calendar_meeting_changed"
