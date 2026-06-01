"""Tests for the top-level CRM meetings API."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import Response as FastAPIResponse
from src.api.routers.crm.crm_meetings_handler import (
    invite_recording_bot,
    list_crm_meetings,
    remove_recording_bot,
)
from src.api.schemas.crm_meeting_models import (
    CRMMeetingListItem,
    CRMMeetingRecordingState,
    CRMMeetingsPage,
)
from src.service.crm.access.meetings_access_service import CRMMeetingsAccessServiceError


class FakeCRMMeetingsAccessService:
    """Route-level fake that enforces the action ownership boundary."""

    def __init__(self, visible_event_ids: set[UUID]):
        self.visible_event_ids = visible_event_ids
        self.closed = False
        self.list_kwargs = {}

    async def close(self) -> None:
        self.closed = True

    async def list_meetings(self, **kwargs) -> CRMMeetingsPage:
        self.list_kwargs = kwargs
        event_id = next(iter(self.visible_event_ids))
        return CRMMeetingsPage(
            items=[
                CRMMeetingListItem(
                    event_id=event_id,
                    title="Customer kickoff",
                    start_at=datetime.now(UTC) + timedelta(hours=1),
                    end_at=datetime.now(UTC) + timedelta(hours=2),
                    status="confirmed",
                    attendee_count=4,
                    linked_deals=[],
                    recording=CRMMeetingRecordingState(),
                    can_invite_bot=True,
                    can_remove_bot=False,
                )
            ],
            page=1,
            limit=50,
            total_count=1,
            total_pages=1,
        )

    async def resolve_visible_event_id(self, event_id: UUID) -> UUID:
        if event_id not in self.visible_event_ids:
            raise CRMMeetingsAccessServiceError("Meeting not found", status_code=404)
        return event_id


class FakeRecordingControlService:
    """Fake provider boundary used by route tests."""

    def __init__(self) -> None:
        self.invited_event_id: UUID | None = None
        self.cancelled_event_id: UUID | None = None

    async def invite_bot(self, event_id: UUID) -> dict[str, str]:
        self.invited_event_id = event_id
        return {"status": "sent", "calendar_event_id": str(event_id)}

    async def cancel_bot(self, event_id: UUID) -> dict[str, str]:
        self.cancelled_event_id = event_id
        return {"status": "cancelled", "calendar_event_id": str(event_id)}


@pytest.fixture
def crm_meetings_route_services():
    """Create route-level fakes without booting the whole FastAPI app."""
    visible_event_id = uuid4()
    access_service = FakeCRMMeetingsAccessService({visible_event_id})
    recording_service = FakeRecordingControlService()
    return {
        "visible_event_id": visible_event_id,
        "access_service": access_service,
        "recording_service": recording_service,
    }


@pytest.mark.asyncio
async def test_list_crm_meetings_uses_safe_response_schema(
    crm_meetings_route_services,
) -> None:
    """List response excludes raw calendar/provider payload fields."""
    response = FastAPIResponse()

    result = await list_crm_meetings(
        response=response,
        service=crm_meetings_route_services["access_service"],
    )

    assert response.status_code == 200
    item = result.data.items[0]
    serialized = item.model_dump()
    assert item.event_id == crm_meetings_route_services["visible_event_id"]
    assert "data_payload" not in serialized
    assert "provider_payload" not in serialized["recording"]
    assert "attendees" not in serialized


@pytest.mark.asyncio
async def test_list_crm_meetings_allows_large_month_window_limit(
    crm_meetings_route_services,
) -> None:
    """Month views can request a full visible calendar window without page loss at 100 rows."""
    response = FastAPIResponse()
    start_from = datetime(2026, 6, 1, tzinfo=UTC)
    start_to = datetime(2026, 7, 13, tzinfo=UTC)

    await list_crm_meetings(
        response=response,
        page=1,
        limit=500,
        start_from=start_from,
        start_to=start_to,
        time_scope=None,
        recording_status=None,
        linked_deal=None,
        service=crm_meetings_route_services["access_service"],
    )

    assert response.status_code == 200
    assert crm_meetings_route_services["access_service"].list_kwargs == {
        "page": 1,
        "limit": 500,
        "start_from": start_from,
        "start_to": start_to,
        "time_scope": None,
        "recording_status": None,
        "linked_deal": None,
    }


@pytest.mark.asyncio
async def test_invite_recording_bot_requires_user_visible_event(
    crm_meetings_route_services,
) -> None:
    """Provider dispatch is reached only after the event resolver accepts the id."""
    visible_event_id = crm_meetings_route_services["visible_event_id"]
    response = FastAPIResponse()

    result = await invite_recording_bot(
        event_id=visible_event_id,
        response=response,
        access_service=crm_meetings_route_services["access_service"],
        recording_service=crm_meetings_route_services["recording_service"],
    )

    assert response.status_code == 200
    assert result.data["status"] == "sent"
    assert crm_meetings_route_services["recording_service"].invited_event_id == visible_event_id


@pytest.mark.asyncio
async def test_invite_recording_bot_rejects_other_user_event(
    crm_meetings_route_services,
) -> None:
    """A guessed raw event UUID does not reach provider dispatch."""
    response = FastAPIResponse()

    result = await invite_recording_bot(
        event_id=uuid4(),
        response=response,
        access_service=crm_meetings_route_services["access_service"],
        recording_service=crm_meetings_route_services["recording_service"],
    )

    assert response.status_code == 404
    assert result.status == "ERROR"
    assert crm_meetings_route_services["recording_service"].invited_event_id is None


@pytest.mark.asyncio
async def test_remove_recording_bot_requires_user_visible_event(
    crm_meetings_route_services,
) -> None:
    """Delete route resolves event ownership before reaching provider cancellation."""
    visible_event_id = crm_meetings_route_services["visible_event_id"]
    response = FastAPIResponse()

    result = await remove_recording_bot(
        event_id=visible_event_id,
        response=response,
        access_service=crm_meetings_route_services["access_service"],
        recording_service=crm_meetings_route_services["recording_service"],
    )

    assert response.status_code == 200
    assert result.data["status"] == "cancelled"
    assert crm_meetings_route_services["recording_service"].cancelled_event_id == visible_event_id


@pytest.mark.asyncio
async def test_remove_recording_bot_rejects_other_user_event(
    crm_meetings_route_services,
) -> None:
    """A guessed raw event UUID cannot cancel another user's recording bot."""
    response = FastAPIResponse()

    result = await remove_recording_bot(
        event_id=uuid4(),
        response=response,
        access_service=crm_meetings_route_services["access_service"],
        recording_service=crm_meetings_route_services["recording_service"],
    )

    assert response.status_code == 404
    assert result.status == "ERROR"
    assert crm_meetings_route_services["recording_service"].cancelled_event_id is None
