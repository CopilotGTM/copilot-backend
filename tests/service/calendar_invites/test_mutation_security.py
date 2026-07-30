"""Security-boundary tests for modifying existing calendar meetings."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.calendar_invites.models import CalendarInviteError
from src.service.calendar_invites.mutations import CalendarMeetingMutationService


class _RowsResult:
    """Minimal SQLAlchemy result double for mutation target lookup."""

    def __init__(self, rows: list[tuple[object, object, object]]) -> None:
        self.rows = rows

    def all(self) -> list[tuple[object, object, object]]:
        """Return the configured joined query rows."""
        return self.rows


class _TargetSession:
    """Session double returning a single calendar ownership candidate."""

    def __init__(self, row: tuple[object, object, object]) -> None:
        self.row = row

    async def execute(self, statement) -> _RowsResult:
        """Return the candidate without requiring a database connection."""
        return _RowsResult([self.row])


@pytest.mark.asyncio
async def test_attendee_cannot_modify_another_organizers_meeting() -> None:
    """A connected attendee mailbox must not gain organizer mutation rights."""
    event = SimpleNamespace(
        data_payload={},
        occurrence_anchor=None,
        is_all_day=False,
        organiser_email="owner@example.com",
    )
    state = SimpleNamespace(series_external_id=None)
    integration = SimpleNamespace(
        integration_email="attendee@example.com",
        integration_type=IntegrationType.GOOGLE,
    )
    service = CalendarMeetingMutationService(
        _TargetSession((event, state, integration)),
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        requested_by_user_id=UUID("22222222-2222-2222-2222-222222222222"),
    )

    with pytest.raises(CalendarInviteError) as exc_info:
        await service.resolve_target(UUID("33333333-3333-3333-3333-333333333333"))

    assert exc_info.value.code == "calendar_organizer_required"
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_snapshot_fetches_organizer_copy_instead_of_shared_etag() -> None:
    """Mailbox state must select the provider copy that supplies the mutation ETag."""

    event = SimpleNamespace(
        data_payload={"etag": '"attendee-copy-etag"'},
        occurrence_anchor=None,
        is_all_day=False,
        organiser_email="owner@example.com",
        attendees=[],
    )
    state = SimpleNamespace(
        external_id="organizer-provider-event",
        series_external_id=None,
    )
    integration = SimpleNamespace(
        integration_email="owner@example.com",
        integration_type=IntegrationType.GOOGLE,
    )
    fetched_ids: list[str] = []

    class FakeResolver:
        """Return inert token data for the organizer integration."""

        async def resolve(self, *args, **kwargs):
            """Return a token without performing account resolution."""
            return SimpleNamespace(token_info=SimpleNamespace())

    class FakeGoogleClient:
        """Return the organizer payload while recording its mailbox event ID."""

        async def get_event(self, token_info, provider_event_id):
            """Fetch the organizer copy identified by user state."""
            fetched_ids.append(provider_event_id)
            return {
                "id": provider_event_id,
                "iCalUID": "shared-event@example.com",
                "etag": '"organizer-copy-etag"',
                "updated": "2026-07-30T10:00:00Z",
                "summary": "Discussion",
                "status": "confirmed",
                "organizer": {"email": "owner@example.com", "self": True},
                "start": {"dateTime": "2026-08-01T10:00:00Z", "timeZone": "UTC"},
                "end": {"dateTime": "2026-08-01T10:30:00Z", "timeZone": "UTC"},
                "attendees": [],
            }

    service = CalendarMeetingMutationService(
        _TargetSession((event, state, integration)),
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        requested_by_user_id=UUID("22222222-2222-2222-2222-222222222222"),
        integration_resolver=FakeResolver(),
        google_client=FakeGoogleClient(),
    )

    snapshot = await service.resolve_snapshot(UUID("33333333-3333-3333-3333-333333333333"))

    assert fetched_ids == ["organizer-provider-event"]
    assert snapshot.provider_etag == '"organizer-copy-etag"'
