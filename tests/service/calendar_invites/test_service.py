"""Tests for calendar invite service orchestration and persistence side effects."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from src.domain.models.organisation_tables.sync import CalendarInviteRequestStatus
from src.service.calendar_invites.models import CalendarInviteAttendee, CalendarInviteCreateRequest
from src.service.calendar_invites.service import CalendarInviteService, derive_idempotency_key


class _FakeSession:
    """In-memory session double that tracks writes and transaction calls."""

    def __init__(self) -> None:
        self.added: list[object] = []
        self.flushed = 0
        self.committed = 0
        self.rolled_back = 0

    def add(self, value: object) -> None:
        """Track each staged ORM row for later assertion."""
        self.added.append(value)

    async def flush(self) -> None:
        """Track flush calls used to verify transactional sequencing."""
        self.flushed += 1

    async def commit(self) -> None:
        """Track commit calls so ordering can be asserted."""
        self.committed += 1

    async def rollback(self) -> None:
        """Track rollback calls so failure branches stay observable."""
        self.rolled_back += 1

    async def execute(self, statement):
        """Return empty query results to avoid touching a real database."""

        class _Scalars:
            """Scalar accessor used by repository-style query result shapes."""

            def first(self):
                """Return first row or None for empty result sets."""
                return None

        class _Result:
            """Return object for `session.execute` result shape in repository code."""

            def scalars(self):
                """Return the scalar accessor so `.execute().scalars().first()` remains valid."""
                return _Scalars()

        return _Result()


class _FakeResolver:
    """Resolver stub returning a deterministic usable sender integration."""

    async def resolve(self, session, organisation_id, requested_by_user_id, *, sender_email=None):
        """Return a valid resolved integration payload for happy-path assertions."""
        return SimpleNamespace(
            integration=SimpleNamespace(
                integration_id=UUID("33333333-3333-3333-3333-333333333333"),
                integration_type="outlook_calendar",
            ),
            token_info=SimpleNamespace(access_token="token"),
        )


class _FakeGraphClient:
    """Graph client stub that echoes a deterministic event response."""

    def __init__(self) -> None:
        self.requests: list[CalendarInviteCreateRequest] = []
        self.commit_counts_before_call: list[int] = []

    async def create_event(self, token_info, request: CalendarInviteCreateRequest):
        """Record the request and return a synthetic provider payload."""
        self.requests.append(request)
        return {
            "id": "provider-event-1",
            "iCalUId": "icaluid-1",
            "subject": request.title,
            "start": {"dateTime": "2026-05-12T20:00:00", "timeZone": "India Standard Time"},
            "end": {"dateTime": "2026-05-12T20:30:00", "timeZone": "India Standard Time"},
            "attendees": [
                {
                    "emailAddress": {"address": "buyer@example.com", "name": "Buyer"},
                    "status": {"response": "none"},
                }
            ],
            "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/meetup-join/abc"},
        }


class _FakeWriter:
    """Calendar writer stub used to verify event and attendee sync ordering."""

    def __init__(self, session, organisation_id) -> None:
        self.calls: list[str] = []

    async def upsert_event(self, integration, normalized):
        """Record event sync and return a synthetic calendar event wrapper."""
        self.calls.append("upsert_event")
        return SimpleNamespace(
            event_id=UUID("44444444-4444-4444-4444-444444444444"),
            external_id=normalized.identity.external_id,
        )

    async def upsert_user_state(self, event, integration, external_id):
        """Record user-state sync call for service-side assertions."""
        self.calls.append("upsert_user_state")

    async def sync_attendees(self, event, normalized):
        """Record attendee-sync call for ordering assertions."""
        self.calls.append("sync_attendees")


@pytest.mark.asyncio
async def test_create_invite_audits_graph_result_and_syncs_calendar_event() -> None:
    """Successful service flow should persist audit status and sync event state."""
    # Contract: successful invite flow stores audit row, syncs event+attendees, and
    # commits only once after all mutations are complete.
    session = _FakeSession()
    graph_client = _FakeGraphClient()
    writers: list[_FakeWriter] = []

    def writer_factory(session_arg, organisation_id):
        """Create and capture a fake writer for assertion of call order."""
        writer = _FakeWriter(session_arg, organisation_id)
        writers.append(writer)
        return writer

    service = CalendarInviteService(
        session,
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        requested_by_user_id=UUID("22222222-2222-2222-2222-222222222222"),
        integration_resolver=_FakeResolver(),
        graph_client=graph_client,
        writer_factory=writer_factory,
    )

    result = await service.create_invite(
        CalendarInviteCreateRequest(
            organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
            requested_by_user_id=UUID("22222222-2222-2222-2222-222222222222"),
            title="Discovery call",
            start_at=datetime(2026, 5, 12, 14, 30, tzinfo=UTC),
            end_at=datetime(2026, 5, 12, 15, 0, tzinfo=UTC),
            timezone="Asia/Kolkata",
            attendees=[CalendarInviteAttendee(email="buyer@example.com", name="Buyer")],
            sender_email="seller@contoso.com",
            idempotency_key="invite-123",
        )
    )

    audit_row = session.added[0]
    assert audit_row.status == CalendarInviteRequestStatus.SENT.value
    assert audit_row.provider_event_id == "provider-event-1"
    assert audit_row.event_id == UUID("44444444-4444-4444-4444-444444444444")
    assert result.provider_event_id == "provider-event-1"
    assert writers[0].calls == ["upsert_event", "upsert_user_state", "sync_attendees"]
    assert session.committed == 2
    assert session.rolled_back == 0


@pytest.mark.asyncio
async def test_create_invite_commits_audit_row_before_graph_call() -> None:
    """Graph should not be called until SENDING audit state is durable."""
    session = _FakeSession()
    graph_client = _FakeGraphClient()

    async def create_event_after_audit_commit(token_info, request: CalendarInviteCreateRequest):
        """Assert transaction ordering at the external-call boundary."""
        assert session.committed == 1
        return await _FakeGraphClient.create_event(graph_client, token_info, request)

    graph_client.create_event = create_event_after_audit_commit

    service = CalendarInviteService(
        session,
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        requested_by_user_id=UUID("22222222-2222-2222-2222-222222222222"),
        integration_resolver=_FakeResolver(),
        graph_client=graph_client,
        writer_factory=_FakeWriter,
    )

    await service.create_invite(
        CalendarInviteCreateRequest(
            organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
            requested_by_user_id=UUID("22222222-2222-2222-2222-222222222222"),
            title="Discovery call",
            start_at=datetime(2026, 5, 12, 14, 30, tzinfo=UTC),
            end_at=datetime(2026, 5, 12, 15, 0, tzinfo=UTC),
            timezone="Asia/Kolkata",
            attendees=[CalendarInviteAttendee(email="buyer@example.com", name="Buyer")],
            sender_email="seller@contoso.com",
        )
    )

    assert session.committed == 2


def test_derive_idempotency_key_includes_selected_sender_email() -> None:
    """Same meeting details from different sender accounts should not collide."""
    base_request = CalendarInviteCreateRequest(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        requested_by_user_id=UUID("22222222-2222-2222-2222-222222222222"),
        title="Discovery call",
        start_at=datetime(2026, 5, 12, 14, 30, tzinfo=UTC),
        end_at=datetime(2026, 5, 12, 15, 0, tzinfo=UTC),
        timezone="Asia/Kolkata",
        attendees=[CalendarInviteAttendee(email="buyer@example.com", name="Buyer")],
        sender_email="seller@contoso.com",
    )

    other_sender_request = base_request.model_copy(update={"sender_email": "ae@contoso.com"})

    assert derive_idempotency_key(base_request) != derive_idempotency_key(other_sender_request)
