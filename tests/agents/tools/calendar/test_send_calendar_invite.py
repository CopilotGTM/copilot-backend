"""Tests for the agent calendar invite tool orchestration."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from src.agents.tools.calendar.send_calendar_invite import send_calendar_invite
from src.service.calendar_invites.attendee_resolver import (
    AttendeeCandidate,
    AttendeeResolutionResult,
)
from src.service.calendar_invites.models import CalendarInviteCreateResult


class _AsyncSessionContext:
    """Async session context wrapper used to verify context wiring."""

    def __init__(self, session: MagicMock) -> None:
        self.session = session

    async def __aenter__(self) -> MagicMock:
        """Enter and expose the fake SQL session."""
        return self.session

    async def __aexit__(self, *_exc: object) -> bool:
        """Exit without suppressing errors to mimic async context behavior."""
        return False


@dataclass
class _FakeContext:
    organisation_id: UUID
    user_id: UUID | None
    channel: str = "teams"
    metadata: dict = field(default_factory=dict)

    @asynccontextmanager
    async def acquire_session(self):
        """Return a fake SQL session from the async protocol."""
        yield MagicMock()


@pytest.mark.asyncio
async def test_send_calendar_invite_creates_invite_for_current_user(monkeypatch) -> None:
    """Happy-path tool call should call service with normalized request values."""
    # Contract: when context has a user, tool should call service with localized
    # timezone and normalized attendee payload.
    session = MagicMock()
    context = _FakeContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        metadata={"user_timezone": "Asia/Kolkata"},
    )
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(session))
    created_requests = []

    class FakeService:
        """Service substitute that captures CalendarInviteCreateRequest values."""

        def __init__(self, session_arg, *, organisation_id, requested_by_user_id):
            """Validate wiring from tool context into service instantiation."""
            assert session_arg is session
            assert organisation_id == context.organisation_id
            assert requested_by_user_id == context.user_id

        async def create_invite(self, request):
            """Record request and return a synthetic successful invite result."""
            created_requests.append(request)
            return CalendarInviteCreateResult(
                request_id=UUID("33333333-3333-3333-3333-333333333333"),
                event_id=UUID("44444444-4444-4444-4444-444444444444"),
                provider_event_id="provider-event-1",
                subject=request.title,
                conference_link="https://teams.microsoft.com/l/meetup-join/abc",
            )

    monkeypatch.setattr(
        "src.agents.tools.calendar.send_calendar_invite.CalendarInviteService",
        FakeService,
    )

    result = await send_calendar_invite(
        context=context,
        title="Discovery call",
        start_time="2026-05-12T20:00:00+05:30",
        end_time="2026-05-12T20:30:00+05:30",
        attendees=["buyer@example.com"],
        sender_email="seller@contoso.com",
    )

    assert result["status"] == "sent"
    assert result["request_id"] == "33333333-3333-3333-3333-333333333333"
    assert result["activity_request_id"] == "33333333-3333-3333-3333-333333333333"
    assert result["activity_type"] == "send_calendar_invite"
    assert result["linked_record_type"] == "calendar_event"
    assert result["linked_record_id"] == "44444444-4444-4444-4444-444444444444"
    assert result["provider_resource_id"] == "provider-event-1"
    assert result["provider_event_id"] == "provider-event-1"
    assert result["event_id"] == "44444444-4444-4444-4444-444444444444"
    assert created_requests[0].timezone == "Asia/Kolkata"
    assert created_requests[0].attendees[0].email == "buyer@example.com"
    assert created_requests[0].sender_email == "seller@contoso.com"


@pytest.mark.asyncio
async def test_send_calendar_invite_requires_current_user() -> None:
    """Tool must reject calls when agent context is missing user identity."""
    # Contract: missing sender identity must block invite flow before any service call.
    result = await send_calendar_invite(
        context=_FakeContext(
            organisation_id=UUID("11111111-1111-1111-1111-111111111111"), user_id=None
        ),
        title="Discovery call",
        start_time="2026-05-12T20:00:00+05:30",
        end_time="2026-05-12T20:30:00+05:30",
        attendees=["buyer@example.com"],
    )

    assert result["status"] == "error"
    assert "user" in result["message"].lower()


@pytest.mark.asyncio
async def test_send_calendar_invite_requires_confirmation_for_resolved_name(
    monkeypatch,
) -> None:
    """Name-derived attendees must be confirmed before Graph is called."""
    session = MagicMock()
    context = _FakeContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        metadata={"user_timezone": "Asia/Kolkata"},
    )
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(session))

    class FakeResolver:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def resolve(self, attendees, *, deal_id=None):
            return AttendeeResolutionResult(
                resolved=[
                    AttendeeCandidate(
                        email="saksham@example.com",
                        name="Saksham Bhutani",
                        source="contact",
                        source_id="contact-1",
                        confidence="exact_name",
                        input_value="Saksham Bhutani",
                    )
                ],
                requires_confirmation=True,
                confirmation_reasons=["attendee_resolved_from_name"],
            )

    service = MagicMock()
    monkeypatch.setattr(
        "src.agents.tools.calendar.send_calendar_invite.CalendarInviteService",
        service,
    )
    monkeypatch.setattr(
        "src.agents.tools.calendar.send_calendar_invite.CalendarInviteAttendeeResolver",
        FakeResolver,
    )

    result = await send_calendar_invite(
        context=context,
        title="Discovery call",
        start_time="2026-05-12T20:00:00+05:30",
        end_time="2026-05-12T20:30:00+05:30",
        attendees=["Saksham Bhutani"],
        sender_email="seller@contoso.com",
    )

    assert result["status"] == "confirmation_required"
    assert result["confirmation_payload"]["attendees"] == [
        {
            "email": "saksham@example.com",
            "name": "Saksham Bhutani",
            "optional": False,
            "source": "contact",
            "source_id": "contact-1",
            "confidence": "exact_name",
            "input_value": "Saksham Bhutani",
        }
    ]
    service.assert_not_called()


@pytest.mark.asyncio
async def test_send_calendar_invite_blocks_ambiguous_attendees(monkeypatch) -> None:
    """Ambiguous people should ask for clarification and skip side effects."""
    session = MagicMock()
    context = _FakeContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        metadata={"user_timezone": "Asia/Kolkata"},
    )
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(session))

    class FakeResolver:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def resolve(self, attendees, *, deal_id=None):
            return AttendeeResolutionResult(
                ambiguous=[
                    {
                        "input": "Saksham",
                        "options": [
                            {"email": "saksham.one@example.com", "name": "Saksham One"},
                            {"email": "saksham.two@example.com", "name": "Saksham Two"},
                        ],
                    }
                ],
                requires_confirmation=True,
                confirmation_reasons=["attendee_ambiguous"],
            )

    service = MagicMock()
    monkeypatch.setattr(
        "src.agents.tools.calendar.send_calendar_invite.CalendarInviteService",
        service,
    )
    monkeypatch.setattr(
        "src.agents.tools.calendar.send_calendar_invite.CalendarInviteAttendeeResolver",
        FakeResolver,
    )

    result = await send_calendar_invite(
        context=context,
        title="Discovery call",
        start_time="2026-05-12T20:00:00+05:30",
        end_time="2026-05-12T20:30:00+05:30",
        attendees=["Saksham"],
        sender_email="seller@contoso.com",
    )

    assert result["status"] == "needs_clarification"
    assert result["code"] == "calendar_attendees_need_clarification"
    assert result["ambiguous_attendees"][0]["input"] == "Saksham"
    service.assert_not_called()
