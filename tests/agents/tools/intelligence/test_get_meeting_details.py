"""Regression tests for user-owned meeting details and attendee classification."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from src.agents.context import AgentContext
from src.agents.tools.intelligence.get_meeting_details import get_meeting_details


class _AsyncSessionContext:
    def __init__(self, session: Any) -> None:
        self.session = session

    async def __aenter__(self) -> Any:
        return self.session

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _ScalarResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Any:
        """Return the configured scalar like a SQLAlchemy scalar result."""
        return self.value


class _CaptureSession:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.statements: list[Any] = []

    async def execute(self, stmt: Any) -> _ScalarResult:
        """Capture each statement and return its configured scalar response."""
        self.statements.append(stmt)
        return _ScalarResult(self.responses.pop(0))


class _CalendarOwnershipOnlySession:
    """Expose an event through user ownership but not through a deal association."""

    def __init__(self, event: Any) -> None:
        self.event = event

    async def execute(self, stmt: Any) -> _ScalarResult:
        """Return the event only when the query uses the user's calendar boundary."""
        sql = str(stmt)
        if "calendar_event_user_states" in sql:
            return _ScalarResult(self.event)
        return _ScalarResult(None)


def _calendar_event(event_id: UUID) -> Any:
    return SimpleNamespace(
        event_id=event_id,
        title="Customer planning",
        description=None,
        start_at=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
        end_at=datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
        organiser_name="Seller",
        organiser_email="seller@our-company.example",
        attendees=[
            SimpleNamespace(
                email="seller@our-company.example",
                display_name="Seller",
                response_status="accepted",
                is_organiser=True,
            ),
            SimpleNamespace(
                email="buyer@customer.example",
                display_name="Buyer",
                response_status="tentative",
                is_organiser=False,
            ),
        ],
        attendee_count=2,
        location=None,
        conference_link=None,
        status="confirmed",
        is_all_day=False,
    )


@pytest.mark.asyncio
async def test_get_meeting_details_requires_current_user() -> None:
    """An event UUID must never bypass the current user's calendar boundary."""
    context = AgentContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=None,
    )
    context.acquire_session = MagicMock()  # type: ignore[method-assign]

    result = await get_meeting_details(str(uuid4()), context=context)

    assert result is None
    context.acquire_session.assert_not_called()


@pytest.mark.asyncio
async def test_get_meeting_details_scopes_event_and_classifies_invitees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owned event details should mark internal invitees without claiming attendance."""
    event_id = uuid4()
    session = _CaptureSession([_calendar_event(event_id), None, None])
    context = AgentContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
    )
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(session))  # type: ignore[method-assign]
    internal_domains = AsyncMock(return_value={"our-company.example"})
    monkeypatch.setattr(
        "src.agents.tools.intelligence.get_meeting_details.get_internal_domains",
        internal_domains,
    )

    result = await get_meeting_details(str(event_id), context=context)

    assert result is not None
    assert [attendee["is_internal"] for attendee in result["attendees"]] == [True, False]
    event_sql = str(session.statements[0])
    assert "calendar_event_user_states" in event_sql
    assert "knowledge_integrations" in event_sql
    assert "calendar_events.organisation_id" in event_sql
    assert "knowledge_integrations.created_by_user_id" in event_sql
    internal_domains.assert_awaited_once_with(context.organisation_id)


@pytest.mark.asyncio
async def test_get_meeting_details_preserves_userless_deal_scoped_briefing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Automated meeting prep may read only events actively associated to its deal."""
    event_id = uuid4()
    deal_id = uuid4()
    session = _CaptureSession([_calendar_event(event_id), None, None])
    context = AgentContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=None,
        deal_id=deal_id,
    )
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(session))  # type: ignore[method-assign]
    require_access = AsyncMock()
    monkeypatch.setattr(
        "src.agents.tools.intelligence.get_meeting_details.require_agent_deal_access",
        require_access,
    )
    monkeypatch.setattr(
        "src.agents.tools.intelligence.get_meeting_details.get_internal_domains",
        AsyncMock(return_value={"our-company.example"}),
    )

    result = await get_meeting_details(str(event_id), context=context)

    assert result is not None
    event_sql = str(session.statements[0])
    assert "calendar_event_deal_associations" in event_sql
    assert "calendar_event_deal_associations.deal_id" in event_sql
    assert "calendar_event_user_states" not in event_sql
    require_access.assert_awaited_once_with(context, session, deal_id)


@pytest.mark.asyncio
async def test_get_meeting_details_context_deal_rejects_owned_unassociated_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitting deal_id must still enforce the human context deal association."""
    event_id = uuid4()
    deal_id = uuid4()
    session = _CalendarOwnershipOnlySession(_calendar_event(event_id))
    context = AgentContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        deal_id=deal_id,
    )
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(session))  # type: ignore[method-assign]
    require_access = AsyncMock()
    monkeypatch.setattr(
        "src.agents.tools.intelligence.get_meeting_details.require_agent_deal_access",
        require_access,
    )
    monkeypatch.setattr(
        "src.agents.tools.intelligence.get_meeting_details.get_internal_domains",
        AsyncMock(return_value={"our-company.example"}),
    )

    result = await get_meeting_details(str(event_id), context=context)

    assert result is None
    require_access.assert_awaited_once_with(context, session, deal_id)


@pytest.mark.asyncio
async def test_get_meeting_details_keeps_classification_unknown_without_domains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing domain configuration must not misclassify invitees as external."""
    event_id = uuid4()
    session = _CaptureSession([_calendar_event(event_id), None, None])
    context = AgentContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
    )
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(session))  # type: ignore[method-assign]
    monkeypatch.setattr(
        "src.agents.tools.intelligence.get_meeting_details.get_internal_domains",
        AsyncMock(return_value=set()),
    )

    result = await get_meeting_details(str(event_id), context=context)

    assert result is not None
    assert [attendee["is_internal"] for attendee in result["attendees"]] == [None, None]
