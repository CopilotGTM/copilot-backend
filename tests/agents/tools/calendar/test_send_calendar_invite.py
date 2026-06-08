"""Tests for the agent calendar invite tool orchestration."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from src.agents.tools.calendar.send_calendar_invite import send_calendar_invite
from src.service.calendar_invites.attendee_resolver import (
    AttendeeCandidate,
    AttendeeResolutionResult,
)
from src.service.calendar_invites.models import CalendarInviteCreateResult, CalendarInviteError


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
        confirmed=True,
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
async def test_send_calendar_invite_requires_final_confirmation_before_sending(
    monkeypatch,
) -> None:
    """Complete invite details should still render a final confirmation before sending."""
    session = MagicMock()
    context = _FakeContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        metadata={"user_timezone": "Asia/Kolkata"},
    )
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(session))
    service = MagicMock()
    monkeypatch.setattr(
        "src.agents.tools.calendar.send_calendar_invite.CalendarInviteService",
        service,
    )

    result = await send_calendar_invite(
        context=context,
        title="Meeting with Saksham",
        start_time="2026-05-12T16:00:00+05:30",
        end_time="2026-05-12T17:00:00+05:30",
        attendees=["saksham@copilotgtm.com"],
        sender_email="seller@contoso.com",
    )

    assert result["status"] == "confirmation_required"
    assert result["code"] == "calendar_invite_confirmation_required"
    assert result["confirmation_payload"]["title"] == "Meeting with Saksham"
    assert result["confirmation_payload"]["sender_email"] == "seller@contoso.com"
    assert result["confirmation_payload"]["attendees"][0]["email"] == "saksham@copilotgtm.com"
    assert "final_send_confirmation" in result["confirmation_reasons"]
    service.assert_not_called()


@pytest.mark.asyncio
async def test_send_calendar_invite_replaces_email_based_default_title_before_confirmation(
    monkeypatch,
) -> None:
    """Autogenerated titles must not expose attendee email addresses."""
    session = MagicMock()
    context = _FakeContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        metadata={"user_timezone": "Asia/Kolkata"},
    )
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(session))
    service = MagicMock()
    monkeypatch.setattr(
        "src.agents.tools.calendar.send_calendar_invite.CalendarInviteService",
        service,
    )

    result = await send_calendar_invite(
        context=context,
        title="Meeting with nikhilsinghal005@gmail.com",
        start_time="2026-05-12T17:00:00+05:30",
        end_time="2026-05-12T18:00:00+05:30",
        attendees=["nikhilsinghal005@gmail.com"],
        sender_email="nikhil@copilotgtm.com",
    )

    assert result["status"] == "confirmation_required"
    assert result["confirmation_payload"]["title"] == "Meeting"
    assert result["confirmation_payload"]["attendees"][0]["email"] == ("nikhilsinghal005@gmail.com")
    service.assert_not_called()


@pytest.mark.asyncio
async def test_send_calendar_invite_rejects_yes_as_sender_email(monkeypatch) -> None:
    """A bare approval word must not be accepted as the sender mailbox."""
    session = MagicMock()
    context = _FakeContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        metadata={"user_timezone": "Asia/Kolkata"},
    )
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(session))
    service = MagicMock()
    monkeypatch.setattr(
        "src.agents.tools.calendar.send_calendar_invite.CalendarInviteService",
        service,
    )

    result = await send_calendar_invite(
        context=context,
        title="Meeting",
        start_time="2026-05-12T17:00:00+05:30",
        end_time="2026-05-12T18:00:00+05:30",
        attendees=["nikhilsinghal005@gmail.com"],
        sender_email="Yes",
    )

    assert result["status"] == "needs_clarification"
    assert result["code"] == "calendar_sender_email_invalid"
    assert result["missing_fields"] == ["sender_email"]
    service.assert_not_called()


@pytest.mark.asyncio
async def test_send_calendar_invite_resolves_weekday_time_from_user_timezone_anchor(
    monkeypatch,
) -> None:
    session = MagicMock()
    context = _FakeContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        metadata={
            "user_timezone": "Asia/Kolkata",
            "current_datetime": "2026-04-10T20:30:00+00:00",
        },
    )
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(session))
    created_requests = []

    class FakeService:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def create_invite(self, request):
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
        start_time="Saturday 10 AM",
        end_time="Saturday 10:30 AM",
        attendees=["buyer@example.com"],
        sender_email="seller@contoso.com",
        confirmed=True,
    )

    assert result["status"] == "sent"
    assert created_requests[0].start_at == datetime(2026, 4, 11, 4, 30, tzinfo=UTC)
    assert created_requests[0].end_at == datetime(2026, 4, 11, 5, 0, tzinfo=UTC)
    assert created_requests[0].timezone == "Asia/Kolkata"


@pytest.mark.asyncio
async def test_send_calendar_invite_blocks_tool_timezone_that_conflicts_with_context(
    monkeypatch,
) -> None:
    """The app-captured timezone must not be silently overridden by model-chosen UTC."""
    session = MagicMock()
    context = _FakeContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        metadata={
            "user_timezone": "Asia/Kolkata",
            "current_datetime": "2026-04-10T20:30:00+00:00",
        },
    )
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(session))
    service = MagicMock()
    monkeypatch.setattr(
        "src.agents.tools.calendar.send_calendar_invite.CalendarInviteService",
        service,
    )

    result = await send_calendar_invite(
        context=context,
        title="Discovery call",
        start_time="Saturday 8 PM",
        end_time="Saturday 9 PM",
        attendees=["buyer@example.com"],
        sender_email="seller@contoso.com",
        timezone="UTC",
    )

    assert result["status"] == "confirmation_required"
    assert result["code"] == "calendar_invite_timezone_confirmation_required"
    assert result["confirmation_payload"]["timezone"] == "Asia/Kolkata"
    assert result["confirmation_payload"]["requested_timezone"] == "UTC"
    assert "timezone_conflicts_with_user_timezone" in result["confirmation_reasons"]
    service.assert_not_called()


@pytest.mark.asyncio
async def test_send_calendar_invite_uses_explicit_timezone_after_conflict_confirmation(
    monkeypatch,
) -> None:
    """Confirmed timezone conflicts use the explicit timezone the user approved."""
    session = MagicMock()
    context = _FakeContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        metadata={
            "user_timezone": "Asia/Kolkata",
            "current_datetime": "2026-04-10T20:30:00+00:00",
        },
    )
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(session))
    created_requests = []

    class FakeService:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def create_invite(self, request):
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
        start_time="Saturday 8 PM",
        end_time="Saturday 9 PM",
        attendees=["buyer@example.com"],
        sender_email="seller@contoso.com",
        timezone="UTC",
        confirmed=True,
    )

    assert result["status"] == "sent"
    assert created_requests[0].timezone == "UTC"
    assert created_requests[0].start_at == datetime(2026, 4, 11, 20, 0, tzinfo=UTC)


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
async def test_send_calendar_invite_requires_confirmation_for_resolved_internal_user(
    monkeypatch,
) -> None:
    """Internal teammates resolved from names should not be treated as missing contacts."""
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
            assert attendees == ["Saksham Bhutani"]
            return AttendeeResolutionResult(
                resolved=[
                    AttendeeCandidate(
                        email="saksham@copilotgtm.com",
                        name="Saksham Bhutani",
                        source="internal_user",
                        source_id="user-1",
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
        title="Meeting with Saksham",
        start_time="today 4 PM",
        end_time="today 5 PM",
        attendees=["Saksham Bhutani"],
        sender_email="seller@contoso.com",
    )

    assert result["status"] == "confirmation_required"
    assert result["confirmation_payload"]["attendees"][0]["source"] == "internal_user"
    assert result["confirmation_payload"]["attendees"][0]["email"] == "saksham@copilotgtm.com"
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
    assert result["missing_fields"] == ["attendees"]
    assert result["ambiguous_attendees"][0]["input"] == "Saksham"
    service.assert_not_called()


@pytest.mark.asyncio
async def test_send_calendar_invite_returns_sender_selection_as_structured_clarification(
    monkeypatch,
) -> None:
    """Multiple calendar accounts should produce a bundled sender blocker for the agent."""
    session = MagicMock()
    context = _FakeContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        metadata={"user_timezone": "Asia/Kolkata"},
    )
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(session))

    class FakeService:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def create_invite(self, _request):
            raise CalendarInviteError(
                "calendar_sender_selection_required",
                (
                    "You have multiple Calendar accounts that can send invites. "
                    "Which sender should I use? Available sender accounts: "
                    "seller@contoso.com, ae@contoso.com."
                ),
                status_code=409,
            )

    monkeypatch.setattr(
        "src.agents.tools.calendar.send_calendar_invite.CalendarInviteService",
        FakeService,
    )

    result = await send_calendar_invite(
        context=context,
        title="Meeting with Saksham",
        start_time="2026-05-12T16:00:00+05:30",
        end_time="2026-05-12T17:00:00+05:30",
        attendees=["buyer@example.com"],
        confirmed=True,
    )

    assert result["status"] == "needs_clarification"
    assert result["code"] == "calendar_sender_selection_required"
    assert result["missing_fields"] == ["sender_email"]
    assert result["available_sender_accounts"] == ["seller@contoso.com", "ae@contoso.com"]
