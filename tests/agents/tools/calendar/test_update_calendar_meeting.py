"""Regression tests for calendar meeting preview and ownership boundaries."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from src.agents.tools.calendar.update_calendar_meeting import (
    cancel_calendar_meeting,
    update_calendar_meeting,
)
from src.service.calendar_invites.models import CalendarMeetingMutationResult


@dataclass
class _FakeContext:
    organisation_id: UUID
    user_id: UUID | None
    metadata: dict = field(default_factory=dict)

    @asynccontextmanager
    async def acquire_session(self):
        """Yield the inert session expected by the agent tool."""
        yield object()


def _target(*, provider: str = "microsoft") -> SimpleNamespace:
    event = SimpleNamespace(
        event_id=UUID(int=4),
        title="Customer review",
        start_at=datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
        end_at=datetime(2026, 8, 1, 10, 30, tzinfo=UTC),
        timezone="UTC",
        attendees=[],
        description="Review rollout",
        location="Teams",
        source_modified_at=datetime(2026, 7, 30, 8, 0, tzinfo=UTC),
        data_payload={"@odata.etag": 'W/"version-1"'},
    )
    return SimpleNamespace(
        event=event,
        integration=SimpleNamespace(integration_email="seller@example.com"),
        provider=provider,
    )


@pytest.mark.asyncio
async def test_meeting_update_preview_never_calls_provider_service(monkeypatch) -> None:
    """Resolving and previewing an update must not execute the mutation service."""

    update_calls: list[object] = []

    class FakeService:
        """Resolve a meeting while recording forbidden preview-side writes."""

        def __init__(self, session, *, organisation_id, requested_by_user_id):
            pass

        async def resolve_target(self, event_id, *, sender_email=None):
            """Return the current organizer-owned meeting state."""
            return _target()

        async def update_meeting(self, request, *, sender_email=None):
            """Record an update if the preview guard regresses."""
            update_calls.append(request)

    monkeypatch.setattr(
        "src.agents.tools.calendar.update_calendar_meeting.CalendarMeetingMutationService",
        FakeService,
    )

    result = await update_calendar_meeting(
        context=_FakeContext(UUID(int=1), UUID(int=2)),
        event_id=str(UUID(int=4)),
        title="Updated customer review",
    )

    assert result["status"] == "confirmation_required"
    assert result["confirmation_payload"]["title"] == "Updated customer review"
    assert update_calls == []


@pytest.mark.asyncio
async def test_meeting_cancellation_preview_never_calls_provider_service(monkeypatch) -> None:
    """Resolving and previewing a cancellation must remain read-only."""

    cancel_calls: list[object] = []

    class FakeService:
        """Resolve a meeting while recording forbidden cancellation writes."""

        def __init__(self, session, *, organisation_id, requested_by_user_id):
            pass

        async def resolve_target(self, event_id, *, sender_email=None):
            """Return the meeting selected for cancellation preview."""
            return _target()

        async def cancel_meeting(self, event_id, **kwargs):
            """Record a cancellation if preview unexpectedly mutates."""
            cancel_calls.append(event_id)

    monkeypatch.setattr(
        "src.agents.tools.calendar.update_calendar_meeting.CalendarMeetingMutationService",
        FakeService,
    )

    result = await cancel_calendar_meeting(
        context=_FakeContext(UUID(int=1), UUID(int=2)),
        event_id=str(UUID(int=4)),
    )

    assert result["status"] == "confirmation_required"
    assert result["confirmation_payload"]["title"] == "Customer review"
    assert cancel_calls == []


@pytest.mark.asyncio
async def test_confirmed_meeting_update_uses_preview_version(monkeypatch) -> None:
    """A confirmed update forwards the exact local version used for its preview."""

    requests: list[object] = []

    class FakeService:
        """Capture the version-bound request issued after confirmation."""

        def __init__(self, session, *, organisation_id, requested_by_user_id):
            pass

        async def resolve_target(self, event_id, *, sender_email=None):
            """Return the same meeting state that the preview represents."""
            return _target()

        async def update_meeting(self, request, *, sender_email=None):
            """Capture the update and return a provider-style receipt."""
            requests.append(request)
            return CalendarMeetingMutationResult(
                event_id=request.event_id,
                provider_event_id="provider-event-1",
                status="updated",
            )

    monkeypatch.setattr(
        "src.agents.tools.calendar.update_calendar_meeting.CalendarMeetingMutationService",
        FakeService,
    )

    result = await update_calendar_meeting(
        context=_FakeContext(UUID(int=1), UUID(int=2)),
        event_id=str(UUID(int=4)),
        title="Updated customer review",
        confirmed=True,
    )

    assert result["status"] == "updated"
    assert requests[0].expected_version == "2026-07-30T08:00:00+00:00"
    assert requests[0].provider_etag == 'W/"version-1"'
