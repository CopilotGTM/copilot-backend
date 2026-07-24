"""Tests for the agent calendar invite tool orchestration."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
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
    user_email: str | None = "seller@contoso.com"

    @asynccontextmanager
    async def acquire_session(self):
        """Return a fake SQL session from the async protocol."""
        yield MagicMock()


@pytest.fixture(autouse=True)
def _stub_calendar_sender_context(monkeypatch) -> None:
    """Keep tool tests local while preserving the sender-selection contract."""

    async def no_preference(**_kwargs):
        """Return no persisted preference for the default tool-test path."""
        return None

    async def official_domains(_organisation_id):
        """Treat the fixture mailbox domain as the organization domain."""
        return {"contoso.com"}

    class FakeCalendarResolver:
        """Resolve the explicit or primary test mailbox without a database."""

        async def resolve(self, _session, _organisation_id, _user_id, **kwargs):
            """Return one deterministic writable calendar."""
            email = kwargs.get("sender_email") or kwargs.get("user_email") or "seller@contoso.com"
            return SimpleNamespace(
                integration=SimpleNamespace(
                    integration_email=email,
                    integration_id=UUID("99999999-9999-9999-9999-999999999999"),
                ),
                connected_account_count=1,
                selection_reason=(
                    "explicit_sender" if kwargs.get("sender_email") else "primary_user_email"
                ),
                warning=None,
            )

    monkeypatch.setattr(
        "src.agents.tools.calendar.send_calendar_invite.load_preferred_calendar_sender",
        no_preference,
    )
    monkeypatch.setattr(
        "src.agents.tools.calendar.send_calendar_invite.get_internal_domains",
        official_domains,
    )
    monkeypatch.setattr(
        "src.agents.tools.calendar.send_calendar_invite.CalendarIntegrationResolver",
        FakeCalendarResolver,
    )


async def _preview_then_confirm(**kwargs):
    """Confirm with the exact token returned by the immediately preceding preview."""
    preview = await send_calendar_invite(**kwargs)
    assert preview["status"] == "confirmation_required"
    return await send_calendar_invite(
        **kwargs,
        confirmed=True,
        idempotency_key=preview["confirmation_payload"]["confirmation_token"],
    )


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

    result = await _preview_then_confirm(
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
    assert "title" in result["confirmation_payload"]["inferred_fields"]
    service.assert_not_called()


@pytest.mark.asyncio
async def test_send_calendar_invite_derives_title_and_sender_for_email_attendee(
    monkeypatch,
) -> None:
    """Email-only invites should reach review without title or sender questions."""
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
        start_time="Friday 5 PM",
        end_time="Friday 6 PM",
        attendees=["nikhilsinghal005@gmail.com"],
    )

    assert result["status"] == "confirmation_required"
    assert result["confirmation_payload"]["title"] == "Meeting"
    assert result["confirmation_payload"]["sender_email"] == "seller@contoso.com"
    assert result["confirmation_payload"]["show_sender"] is False
    service.assert_not_called()


@pytest.mark.asyncio
async def test_send_calendar_invite_infers_sender_before_confirmation(monkeypatch) -> None:
    """Missing sender should resolve from the user's connected calendar."""
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
        title="Customer follow-up",
        start_time="Friday 5 PM",
        end_time="Friday 6 PM",
        attendees=["nikhilsinghal005@gmail.com"],
    )

    assert result["status"] == "confirmation_required"
    assert result["confirmation_payload"]["sender_email"] == "seller@contoso.com"
    assert result["confirmation_payload"]["show_sender"] is False
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
    """Relative weekdays should resolve against the user's local calendar date."""
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
        """Capture the final localized request after exact confirmation."""

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def create_invite(self, request):
            """Record the request and return a successful provider result."""
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

    result = await _preview_then_confirm(
        context=context,
        title="Discovery call",
        start_time="Saturday 10 AM",
        end_time="Saturday 10:30 AM",
        attendees=["buyer@example.com"],
        sender_email="seller@contoso.com",
    )

    assert result["status"] == "sent"
    assert created_requests[0].start_at == datetime(2026, 4, 11, 4, 30, tzinfo=UTC)
    assert created_requests[0].end_at == datetime(2026, 4, 11, 5, 0, tzinfo=UTC)
    assert created_requests[0].timezone == "Asia/Kolkata"


@pytest.mark.asyncio
async def test_send_calendar_invite_honors_explicit_timezone_in_review(
    monkeypatch,
) -> None:
    """An explicitly supplied timezone should override the app default."""
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
    assert result["code"] == "calendar_invite_confirmation_required"
    assert result["confirmation_payload"]["timezone"] == "UTC"
    assert "timezone_conflicts_with_user_timezone" not in result["confirmation_reasons"]
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
        """Capture the request that explicitly overrides the app timezone."""

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def create_invite(self, request):
            """Record the request and return a successful provider result."""
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

    result = await _preview_then_confirm(
        context=context,
        title="Discovery call",
        start_time="Saturday 8 PM",
        end_time="Saturday 9 PM",
        attendees=["buyer@example.com"],
        sender_email="seller@contoso.com",
        timezone="UTC",
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
        """Return one CRM contact for a name-derived attendee."""

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def resolve(self, attendees, *, deal_id=None):
            """Resolve the test name without querying CRM."""
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
        """Return one internal user for a name-derived attendee."""

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def resolve(self, attendees, *, deal_id=None):
            """Resolve the test teammate without querying the user table."""
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
        """Return two plausible identities to exercise clarification."""

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def resolve(self, attendees, *, deal_id=None):
            """Return a deliberately ambiguous attendee result."""
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
async def test_send_calendar_invite_derives_title_duration_and_optional_review(
    monkeypatch,
) -> None:
    """The first review should contain every derived field needed for one-click send."""
    context = _FakeContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        metadata={"user_timezone": "Asia/Kolkata"},
    )
    service = MagicMock()
    monkeypatch.setattr(
        "src.agents.tools.calendar.send_calendar_invite.CalendarInviteService",
        service,
    )

    result = await send_calendar_invite(
        context=context,
        attendees=[
            {"email": "bhawesh@example.com", "name": "Bhawesh"},
            {"email": "legal@example.com", "name": "Legal", "optional": True},
        ],
        start_time="2026-07-25 3:30 PM",
    )

    payload = result["confirmation_payload"]
    start_at = datetime.fromisoformat(payload["start_time"])
    end_at = datetime.fromisoformat(payload["end_time"])
    assert result["status"] == "confirmation_required"
    assert payload["title"] == "Meeting with Bhawesh and 1 others"
    assert start_at.utcoffset() == timedelta(hours=5, minutes=30)
    assert end_at - start_at == timedelta(minutes=30)
    assert payload["required_attendees"][0]["email"] == "bhawesh@example.com"
    assert payload["optional_attendees"][0]["email"] == "legal@example.com"
    assert {"title", "duration_30_minutes", "timezone"} <= set(payload["inferred_fields"])
    assert result["review_text"].splitlines() == [
        "Meeting with Bhawesh and 1 others",
        "25 July, 3:30–4:00 PM IST",
        "To: bhawesh@example.com",
        "Optional: legal@example.com",
        "Reply send to confirm.",
    ]
    assert result["_bot_channel_payload"]["force_override"] is True
    service.assert_not_called()


@pytest.mark.asyncio
async def test_send_calendar_invite_restores_parser_date_for_bare_time(monkeypatch) -> None:
    """A bare time should use the parser-inferred date retained before PR 1188."""
    context = _FakeContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        metadata={
            "user_timezone": "Asia/Kolkata",
            "current_datetime": "2026-07-25T04:00:00+00:00",
        },
    )
    service = MagicMock()
    monkeypatch.setattr(
        "src.agents.tools.calendar.send_calendar_invite.CalendarInviteService",
        service,
    )

    result = await send_calendar_invite(
        context=context,
        attendees=[{"email": "bhawesh@example.com", "name": "Bhawesh"}],
        start_time="3:30 PM",
    )

    assert result["status"] == "confirmation_required"
    assert result["confirmation_payload"]["title"] == "Meeting with Bhawesh"
    assert result["review_text"].splitlines()[1] == "25 July, 3:30–4:00 PM IST"
    service.assert_not_called()


@pytest.mark.parametrize(
    ("start_time", "expected_start"),
    [
        ("in 2 days at 3 PM", datetime(2026, 7, 27, 9, 30, tzinfo=UTC)),
        ("next week at 3 PM", datetime(2026, 8, 1, 9, 30, tzinfo=UTC)),
    ],
)
@pytest.mark.asyncio
async def test_send_calendar_invite_accepts_parser_supported_relative_dates(
    monkeypatch,
    start_time,
    expected_start,
) -> None:
    """Relative dates parsed successfully before PR 1188 must not need clarification."""
    context = _FakeContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        metadata={
            "user_timezone": "Asia/Kolkata",
            "current_datetime": "2026-07-25T04:00:00+00:00",
        },
    )
    service = MagicMock()
    monkeypatch.setattr(
        "src.agents.tools.calendar.send_calendar_invite.CalendarInviteService",
        service,
    )

    result = await send_calendar_invite(
        context=context,
        attendees=["bhawesh@example.com"],
        start_time=start_time,
    )

    assert result["status"] == "confirmation_required"
    parsed_start = datetime.fromisoformat(result["confirmation_payload"]["start_time"])
    assert parsed_start.astimezone(UTC) == expected_start
    service.assert_not_called()


@pytest.mark.asyncio
async def test_send_calendar_invite_asks_for_am_pm_on_bare_clock(monkeypatch) -> None:
    """A natural 12-hour clock must not silently become an AM or PM meeting."""
    context = _FakeContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        metadata={"user_timezone": "Asia/Kolkata"},
    )
    service = MagicMock()
    monkeypatch.setattr(
        "src.agents.tools.calendar.send_calendar_invite.CalendarInviteService",
        service,
    )

    result = await send_calendar_invite(
        context=context,
        attendees=["bhawesh@example.com"],
        start_time="25 July 3:30",
    )

    assert result["status"] == "needs_clarification"
    assert result["missing_fields"] == ["start_time_am_pm"]
    assert result["review_payload"]["timezone"] == "Asia/Kolkata"
    service.assert_not_called()


@pytest.mark.asyncio
async def test_send_calendar_invite_accepts_unambiguous_24_hour_clock(monkeypatch) -> None:
    """A 24-hour clock should use the user's default timezone without another question."""
    context = _FakeContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        metadata={"user_timezone": "Asia/Kolkata"},
    )
    service = MagicMock()
    monkeypatch.setattr(
        "src.agents.tools.calendar.send_calendar_invite.CalendarInviteService",
        service,
    )

    result = await send_calendar_invite(
        context=context,
        attendees=["bhawesh@example.com"],
        start_time="25 July 15:30",
    )

    assert result["status"] == "confirmation_required"
    assert result["confirmation_payload"]["timezone"] == "Asia/Kolkata"
    assert result["review_text"].splitlines()[1] == "25 July, 3:30–4:00 PM IST"
    service.assert_not_called()


@pytest.mark.asyncio
async def test_send_calendar_invite_asks_for_am_pm_on_bare_end_clock(monkeypatch) -> None:
    """An explicit start meridiem must not silently determine a bare end clock."""
    context = _FakeContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        metadata={"user_timezone": "Asia/Kolkata"},
    )
    service = MagicMock()
    monkeypatch.setattr(
        "src.agents.tools.calendar.send_calendar_invite.CalendarInviteService",
        service,
    )

    result = await send_calendar_invite(
        context=context,
        attendees=["bhawesh@example.com"],
        start_time="25 July 3:30 PM",
        end_time="4:00",
    )

    assert result["status"] == "needs_clarification"
    assert result["missing_fields"] == ["end_time_am_pm"]
    service.assert_not_called()


@pytest.mark.asyncio
async def test_send_calendar_invite_asks_again_for_invalid_explicit_timezone(
    monkeypatch,
) -> None:
    """A mistyped explicit timezone must not silently fall back to the user default."""
    context = _FakeContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        metadata={"user_timezone": "Asia/Kolkata"},
    )
    service = MagicMock()
    monkeypatch.setattr(
        "src.agents.tools.calendar.send_calendar_invite.CalendarInviteService",
        service,
    )

    result = await send_calendar_invite(
        context=context,
        attendees=["bhawesh@example.com"],
        start_time="25 July 3:30 PM",
        timezone="Mars/Colony",
    )

    assert result["status"] == "needs_clarification"
    assert result["missing_fields"] == ["timezone"]
    service.assert_not_called()


@pytest.mark.asyncio
async def test_send_calendar_invite_rejects_stale_confirmation_token(monkeypatch) -> None:
    """Changed review fields must not inherit approval from an older invite payload."""
    context = _FakeContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        metadata={"user_timezone": "Asia/Kolkata"},
    )
    service = MagicMock()
    monkeypatch.setattr(
        "src.agents.tools.calendar.send_calendar_invite.CalendarInviteService",
        service,
    )

    result = await send_calendar_invite(
        context=context,
        attendees=["bhawesh@example.com"],
        title="Discussion",
        start_time="2026-07-25 3:30 PM",
        confirmed=True,
        idempotency_key="stale-review",
    )

    assert result["status"] == "confirmation_required"
    assert result["code"] == "calendar_invite_confirmation_stale"
    assert result["confirmation_payload"]["confirmation_token"] != "stale-review"
    service.assert_not_called()


@pytest.mark.asyncio
async def test_send_calendar_invite_shows_automatically_selected_sender_for_multiple_accounts(
    monkeypatch,
) -> None:
    """Multiple calendars should show the selected sender without asking a question."""
    session = MagicMock()
    context = _FakeContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        metadata={"user_timezone": "Asia/Kolkata"},
    )
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(session))

    class MultipleAccountResolver:
        """Expose the automatically selected sender from two calendars."""

        async def resolve(self, *_args, **_kwargs):
            """Return the primary mailbox and mark sender display as relevant."""
            return SimpleNamespace(
                integration=SimpleNamespace(
                    integration_email="seller@contoso.com",
                    integration_id=UUID("99999999-9999-9999-9999-999999999999"),
                ),
                connected_account_count=2,
                selection_reason="primary_user_email",
                warning=None,
            )

    monkeypatch.setattr(
        "src.agents.tools.calendar.send_calendar_invite.CalendarIntegrationResolver",
        MultipleAccountResolver,
    )

    result = await send_calendar_invite(
        context=context,
        title="Meeting with Saksham",
        start_time="2026-05-12T16:00:00+05:30",
        end_time="2026-05-12T17:00:00+05:30",
        attendees=["buyer@example.com"],
    )

    assert result["status"] == "confirmation_required"
    assert result["confirmation_payload"]["sender_email"] == "seller@contoso.com"
    assert result["confirmation_payload"]["show_sender"] is True
    assert "From: seller@contoso.com" in result["review_text"].splitlines()
