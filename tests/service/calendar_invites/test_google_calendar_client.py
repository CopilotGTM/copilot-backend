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
        return await request_fn("google-token")

    async def fake_post_json(client, endpoint, *, json_body, params):
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
