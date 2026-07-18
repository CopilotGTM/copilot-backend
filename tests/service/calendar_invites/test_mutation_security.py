"""Security-boundary tests for modifying existing calendar meetings."""

from __future__ import annotations

from datetime import UTC, datetime
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


def test_stale_preview_version_blocks_calendar_mutation() -> None:
    """A meeting changed after preview must be reviewed again before mutation."""
    event = SimpleNamespace(source_modified_at=datetime(2026, 7, 19, 10, 0, tzinfo=UTC))

    with pytest.raises(CalendarInviteError) as exc_info:
        CalendarMeetingMutationService._validate_version(event, "2026-07-19T09:00:00+00:00")

    assert exc_info.value.code == "calendar_meeting_changed"
    assert exc_info.value.status_code == 409
