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
from src.service.calendar_invites.mutations import CalendarMutationSnapshot
from src.service.integrations.calendar.normalizer import CalendarEventNormalizer


@dataclass
class _FakeContext:
    organisation_id: UUID
    user_id: UUID | None
    metadata: dict = field(default_factory=dict)
    user_email: str | None = None
    channel: str = "whatsapp"

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


def _snapshot(
    *,
    provider: str = "microsoft",
    with_attendee: bool = False,
) -> CalendarMutationSnapshot:
    """Return one organizer provider copy with a mailbox-specific ETag."""

    target = _target(provider=provider)
    target.state = SimpleNamespace(external_id="provider-event-1")
    if provider == "google":
        payload = {
            "id": "provider-event-1",
            "iCalUID": "shared-event@example.com",
            "etag": '"organizer-version-1"',
            "updated": "2026-07-30T08:00:00Z",
            "summary": "Customer review",
            "description": "Review rollout",
            "location": "Meet",
            "status": "confirmed",
            "organizer": {"email": "seller@example.com", "self": True},
            "start": {"dateTime": "2026-08-01T10:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2026-08-01T10:30:00Z", "timeZone": "UTC"},
            "attendees": (
                [
                    {
                        "email": "nikhil@example.com",
                        "displayName": "Nikhil",
                        "responseStatus": "accepted",
                    }
                ]
                if with_attendee
                else []
            ),
        }
        etag = payload["etag"]
    else:
        payload = {
            "id": "provider-event-1",
            "iCalUId": "shared-event@example.com",
            "@odata.etag": 'W/"organizer-version-1"',
            "subject": "Customer review",
            "bodyPreview": "Review rollout",
            "location": {"displayName": "Teams"},
            "isCancelled": False,
            "organizer": {"emailAddress": {"address": "seller@example.com", "name": "Seller"}},
            "start": {"dateTime": "2026-08-01T10:00:00", "timeZone": "UTC"},
            "end": {"dateTime": "2026-08-01T10:30:00", "timeZone": "UTC"},
            "attendees": (
                [
                    {
                        "emailAddress": {
                            "address": "nikhil@example.com",
                            "name": "Nikhil",
                        },
                        "type": "required",
                        "status": {"response": "accepted"},
                    }
                ]
                if with_attendee
                else []
            ),
            "lastModifiedDateTime": "2026-07-30T08:00:00Z",
        }
        etag = payload["@odata.etag"]
    return CalendarMutationSnapshot(
        target=target,
        provider_payload=payload,
        normalized=CalendarEventNormalizer().normalize(payload, provider),
        provider_etag=etag,
    )


@pytest.mark.asyncio
async def test_meeting_update_preview_never_calls_provider_service(monkeypatch) -> None:
    """Preview may read the organizer copy but must never execute its mutation."""

    update_calls: list[object] = []

    class FakeService:
        """Resolve a meeting while recording forbidden preview-side writes."""

        def __init__(self, session, *, organisation_id, requested_by_user_id):
            pass

        async def resolve_snapshot(self, event_id, *, sender_email=None):
            """Return the current organizer-owned provider copy."""
            return _snapshot()

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
    assert result["confirmation_payload"]["expected_version"] == 'W/"organizer-version-1"'
    assert update_calls == []


@pytest.mark.asyncio
async def test_meeting_cancellation_preview_never_calls_provider_service(monkeypatch) -> None:
    """Resolving and previewing a cancellation must remain read-only."""

    cancel_calls: list[object] = []

    class FakeService:
        """Resolve a meeting while recording forbidden cancellation writes."""

        def __init__(self, session, *, organisation_id, requested_by_user_id):
            pass

        async def resolve_snapshot(self, event_id, *, sender_email=None):
            """Return the meeting selected for cancellation preview."""
            return _snapshot()

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
async def test_confirmed_meeting_update_uses_organizer_preview_etag(monkeypatch) -> None:
    """A confirmed update forwards the organizer ETag used for its preview."""

    requests: list[object] = []

    class FakeService:
        """Capture the version-bound request issued after confirmation."""

        def __init__(self, session, *, organisation_id, requested_by_user_id):
            pass

        async def resolve_snapshot(self, event_id, *, sender_email=None):
            """Return the same organizer copy that the preview represents."""
            return _snapshot()

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

    preview = await update_calendar_meeting(
        context=_FakeContext(UUID(int=1), UUID(int=2)),
        event_id=str(UUID(int=4)),
        title="Updated customer review",
    )
    confirmation = preview["confirmation_payload"]
    result = await update_calendar_meeting(
        context=_FakeContext(UUID(int=1), UUID(int=2)),
        event_id=str(UUID(int=4)),
        title=confirmation["title"],
        attendees=confirmation["attendees"],
        expected_version=confirmation["expected_version"],
        notification_scope=confirmation["notification_scope"],
        confirmation_token=confirmation["confirmation_token"],
        confirmed=True,
    )

    assert result["status"] == "updated"
    assert requests[0].expected_version == 'W/"organizer-version-1"'
    assert requests[0].provider_etag == 'W/"organizer-version-1"'
    assert "Title" in result["receipt_text"]
    assert "All attendees" in result["receipt_text"]


@pytest.mark.asyncio
async def test_attendee_only_preview_has_one_cta_and_changed_attendee_default(
    monkeypatch,
) -> None:
    """An Outlook roster-only edit stays concise and defaults to new attendees."""

    class FakeService:
        """Expose one provider snapshot without allowing a write."""

        def __init__(self, session, *, organisation_id, requested_by_user_id):
            pass

        async def resolve_snapshot(self, event_id, *, sender_email=None):
            """Return the organizer copy used by the preview."""
            return _snapshot()

    monkeypatch.setattr(
        "src.agents.tools.calendar.update_calendar_meeting.CalendarMeetingMutationService",
        FakeService,
    )

    result = await update_calendar_meeting(
        context=_FakeContext(UUID(int=1), UUID(int=2)),
        event_id=str(UUID(int=4)),
        attendees=[{"email": "bhavesh@example.com", "optional": True}],
        attendee_action="add",
    )

    assert result["status"] == "confirmation_required"
    assert result["confirmation_payload"]["notification_scope"] == "changed_attendees_only"
    assert result["review_text"].count("Reply *confirm* to apply.") == 1
    assert "Will notify" not in result["review_text"]
    assert "notify all" not in result["review_text"].lower()
    assert "New attendees only (default)" in result["review_text"]
    assert result["_bot_channel_payload"]["content"]["text"] == result["review_text"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "requested", "expected_changes"),
    [
        ("add", "bhavesh@example.com", ["+ Add required: bhavesh@example.com"]),
        ("remove", "nikhil@example.com", ["- Remove required: Nikhil — nikhil@example.com"]),
        (
            "replace",
            "bhavesh@example.com",
            [
                "+ Add required: bhavesh@example.com",
                "- Remove required: Nikhil — nikhil@example.com",
            ],
        ),
    ],
)
async def test_attendee_action_applies_to_live_roster(
    monkeypatch,
    action: str,
    requested: str,
    expected_changes: list[str],
) -> None:
    """Semantic attendee actions must produce the reviewed final roster."""

    class FakeService:
        """Expose an organizer roster without allowing a provider write."""

        def __init__(self, session, *, organisation_id, requested_by_user_id):
            pass

        async def resolve_snapshot(self, event_id, *, sender_email=None):
            """Return the current attendee list used as the action base."""
            return _snapshot(with_attendee=True)

    monkeypatch.setattr(
        "src.agents.tools.calendar.update_calendar_meeting.CalendarMeetingMutationService",
        FakeService,
    )

    result = await update_calendar_meeting(
        context=_FakeContext(UUID(int=1), UUID(int=2)),
        event_id=str(UUID(int=4)),
        attendees=[requested],
        attendee_action=action,
    )

    assert result["confirmation_payload"]["attendees"] == [
        {"email": requested, "name": None, "optional": False}
    ]
    assert all(change in result["review_text"] for change in expected_changes)


@pytest.mark.asyncio
async def test_attendee_update_requires_semantic_action(monkeypatch) -> None:
    """An attendee list without add/remove/replace intent must never imply replacement."""

    class FakeService:
        """Expose the meeting while the tool validates roster intent."""

        def __init__(self, session, *, organisation_id, requested_by_user_id):
            pass

        async def resolve_snapshot(self, event_id, *, sender_email=None):
            """Return the current attendee list."""
            return _snapshot(with_attendee=True)

    monkeypatch.setattr(
        "src.agents.tools.calendar.update_calendar_meeting.CalendarMeetingMutationService",
        FakeService,
    )

    result = await update_calendar_meeting(
        context=_FakeContext(UUID(int=1), UUID(int=2)),
        event_id=str(UUID(int=4)),
        attendees=["bhavesh@example.com"],
    )

    assert result["status"] == "needs_clarification"
    assert result["code"] == "calendar_attendee_action_required"


@pytest.mark.asyncio
async def test_remove_confirmation_replays_operand_not_final_roster(monkeypatch) -> None:
    """Confirming removal must still remove the requested person from the live roster."""

    requests: list[object] = []

    class FakeService:
        """Expose one attendee and capture the confirmed final request."""

        def __init__(self, session, *, organisation_id, requested_by_user_id):
            pass

        async def resolve_snapshot(self, event_id, *, sender_email=None):
            """Return the unchanged organizer roster for preview and confirmation."""
            return _snapshot(with_attendee=True)

        async def update_meeting(self, request, *, sender_email=None):
            """Capture the final provider request after token validation."""
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

    preview = await update_calendar_meeting(
        context=_FakeContext(UUID(int=1), UUID(int=2)),
        event_id=str(UUID(int=4)),
        attendees=["nikhil@example.com"],
        attendee_action="remove",
    )
    confirmation = preview["confirmation_payload"]
    result = await update_calendar_meeting(
        context=_FakeContext(UUID(int=1), UUID(int=2)),
        event_id=str(UUID(int=4)),
        attendees=confirmation["attendees"],
        attendee_action=confirmation["attendee_action"],
        expected_version=confirmation["expected_version"],
        notification_scope=confirmation["notification_scope"],
        confirmation_token=confirmation["confirmation_token"],
        confirmed=True,
    )

    assert result["status"] == "updated"
    assert requests[0].attendees == []


@pytest.mark.asyncio
async def test_confirmation_token_rejects_payload_drift(monkeypatch) -> None:
    """Confirmation for one preview must not authorize a different update payload."""

    update_calls: list[object] = []

    class FakeService:
        """Expose a stable meeting while recording forbidden drifted writes."""

        def __init__(self, session, *, organisation_id, requested_by_user_id):
            pass

        async def resolve_snapshot(self, event_id, *, sender_email=None):
            """Return the same provider version for both calls."""
            return _snapshot()

        async def update_meeting(self, request, *, sender_email=None):
            """Record a write if token binding regresses."""
            update_calls.append(request)

    monkeypatch.setattr(
        "src.agents.tools.calendar.update_calendar_meeting.CalendarMeetingMutationService",
        FakeService,
    )

    preview = await update_calendar_meeting(
        context=_FakeContext(UUID(int=1), UUID(int=2)),
        event_id=str(UUID(int=4)),
        title="Reviewed title",
    )
    confirmation = preview["confirmation_payload"]
    result = await update_calendar_meeting(
        context=_FakeContext(UUID(int=1), UUID(int=2)),
        event_id=str(UUID(int=4)),
        title="Different title",
        expected_version=confirmation["expected_version"],
        notification_scope=confirmation["notification_scope"],
        confirmation_token=confirmation["confirmation_token"],
        confirmed=True,
    )

    assert result["status"] == "confirmation_required"
    assert (
        result["confirmation_payload"]["confirmation_token"] != confirmation["confirmation_token"]
    )
    assert update_calls == []


@pytest.mark.asyncio
async def test_google_preview_defaults_to_all_attendees(monkeypatch) -> None:
    """Google uses its supported all-attendee default without a second prompt."""

    class FakeService:
        """Expose one Google provider snapshot without allowing a write."""

        def __init__(self, session, *, organisation_id, requested_by_user_id):
            pass

        async def resolve_snapshot(self, event_id, *, sender_email=None):
            """Return the organizer copy used by the preview."""
            return _snapshot(provider="google")

    monkeypatch.setattr(
        "src.agents.tools.calendar.update_calendar_meeting.CalendarMeetingMutationService",
        FakeService,
    )

    result = await update_calendar_meeting(
        context=_FakeContext(UUID(int=1), UUID(int=2), channel="web"),
        event_id=str(UUID(int=4)),
        title="Updated customer review",
    )

    assert result["confirmation_payload"]["notification_scope"] == "all_attendees"
    assert "All attendees (Google default)" in result["review_text"]
    assert result["ui_card"]["kind"] == "calendar_meeting_update_review"


@pytest.mark.asyncio
async def test_outlook_subject_change_defaults_to_all_attendees(monkeypatch) -> None:
    """A material Outlook edit must not offer selective notification."""

    class FakeService:
        """Expose one Outlook provider snapshot without allowing a write."""

        def __init__(self, session, *, organisation_id, requested_by_user_id):
            pass

        async def resolve_snapshot(self, event_id, *, sender_email=None):
            """Return the organizer copy used by the preview."""
            return _snapshot()

    monkeypatch.setattr(
        "src.agents.tools.calendar.update_calendar_meeting.CalendarMeetingMutationService",
        FakeService,
    )

    result = await update_calendar_meeting(
        context=_FakeContext(UUID(int=1), UUID(int=2)),
        event_id=str(UUID(int=4)),
        title="Partnership discussion",
    )

    assert result["confirmation_payload"]["notification_scope"] == "all_attendees"
    assert "Customer review → Partnership discussion" in result["review_text"]
    assert result["review_text"].count("Reply *confirm* to apply.") == 1


@pytest.mark.asyncio
async def test_stale_confirmation_returns_latest_preview(monkeypatch) -> None:
    """A stale confirmation must refresh instead of mutating a newer meeting."""

    update_calls: list[object] = []

    class FakeService:
        """Return the latest organizer copy while recording forbidden writes."""

        def __init__(self, session, *, organisation_id, requested_by_user_id):
            pass

        async def resolve_snapshot(self, event_id, *, sender_email=None):
            """Return a version newer than the user's confirmation."""
            return _snapshot()

        async def update_meeting(self, request, *, sender_email=None):
            """Record a write if stale confirmation handling regresses."""
            update_calls.append(request)

    monkeypatch.setattr(
        "src.agents.tools.calendar.update_calendar_meeting.CalendarMeetingMutationService",
        FakeService,
    )

    result = await update_calendar_meeting(
        context=_FakeContext(UUID(int=1), UUID(int=2)),
        event_id=str(UUID(int=4)),
        title="Partnership discussion",
        expected_version='W/"older-version"',
        confirmed=True,
    )

    assert result["status"] == "confirmation_required"
    assert result["code"] == "calendar_meeting_update_refreshed"
    assert result["confirmation_payload"]["expected_version"] == 'W/"organizer-version-1"'
    assert update_calls == []
