"""Regression tests for the unified current-user meeting listing tool.

These tests protect the calendar ownership boundary: a chat user can list only
events synced through their own active calendar integration, never org-wide
calendar rows or another user's events.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from src.agents.context import AgentContext
from src.agents.tools.intelligence.list_meetings_unified import (
    list_meetings,
    normalize_meeting_limit,
)


class _AsyncSessionContext:
    def __init__(self, session: Any) -> None:
        self.session = session

    async def __aenter__(self) -> Any:
        return self.session

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _EmptyResult:
    def all(self) -> list[Any]:
        """Mimic SQLAlchemy's row-result API for an empty query response."""
        return []


class _CaptureSession:
    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, stmt: Any) -> _EmptyResult:
        """Capture the generated statement so tests can inspect query boundaries."""
        self.statements.append(stmt)
        return _EmptyResult()


def test_normalize_meeting_limit_defaults_and_caps() -> None:
    """Default and cap limits before vague prompts can overfill model context."""
    # Vague "show my meetings" prompts should stay compact, while explicit
    # over-large limits are capped before they can fill the model context.
    assert normalize_meeting_limit(None) == 10
    assert normalize_meeting_limit(0) == 1
    assert normalize_meeting_limit(200) == 50


@pytest.mark.asyncio
async def test_list_meetings_requires_user_boundary() -> None:
    """Missing user context must never fall back to organisation-wide meetings."""
    # The tool must not degrade to org-wide calendar access when the chat
    # context is missing a user. That would expose other sellers' calendars.
    context = AgentContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=None,
    )
    context.acquire_session = MagicMock()  # type: ignore[method-assign]

    result = await list_meetings(context=context)

    assert result == []
    context.acquire_session.assert_not_called()


@pytest.mark.asyncio
async def test_list_meetings_query_is_scoped_to_current_users_calendar_integration() -> None:
    """The generated query must anchor ownership on the user's calendar integration."""
    # This locks the core authorization shape: calendar ownership comes from the
    # user's active integration and user-state row, not from attendee email.
    session = _CaptureSession()
    context = AgentContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        metadata={"current_datetime": "2026-05-13T12:00:00+00:00"},
    )
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(session))  # type: ignore[method-assign]

    result = await list_meetings(
        context=context,
        start_datetime=datetime(2026, 5, 13, 12, 0, tzinfo=UTC),
        limit=2,
    )

    assert result == []
    assert len(session.statements) == 1

    sql = str(session.statements[0])
    assert "knowledge_integrations" in sql
    assert "calendar_event_user_states" in sql
    assert "knowledge_integrations.created_by_user_id" in sql
    assert "calendar_event_user_states.is_deleted" in sql
    assert "calendar_events.organisation_id" in sql
    assert "calendar_events.start_at >= " in sql


@pytest.mark.asyncio
async def test_list_meetings_interprets_naive_bounds_in_user_timezone() -> None:
    session = _CaptureSession()
    context = AgentContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        metadata={
            "user_timezone": "Asia/Kolkata",
            "current_datetime": "2026-04-10T20:30:00+00:00",
        },
    )
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(session))  # type: ignore[method-assign]

    await list_meetings(
        context=context,
        start_datetime="2026-04-11T10:00:00",
        end_datetime="2026-04-11T11:00:00",
        limit=2,
    )

    compiled = session.statements[0].compile(compile_kwargs={"literal_binds": True})
    sql = str(compiled)
    assert "2026-04-11 04:30:00+00:00" in sql
    assert "2026-04-11 05:30:00+00:00" in sql


@pytest.mark.asyncio
async def test_list_meetings_uses_past_temporal_metadata() -> None:
    # Past wording from the latest user turn must not silently default to the
    # upcoming-meeting query shape when the model omits explicit bounds.
    session = _CaptureSession()
    context = AgentContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        metadata={
            "current_datetime": "2026-05-13T12:00:00+00:00",
            "temporal_direction": "past",
            "temporal_limit": 5,
        },
    )
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(session))  # type: ignore[method-assign]

    await list_meetings(context=context)

    compiled = session.statements[0].compile(compile_kwargs={"literal_binds": True})
    sql = str(compiled)
    assert "calendar_events.start_at < '2026-05-13 12:00:00+00:00'" in sql
    assert "ORDER BY calendar_events.start_at DESC" in sql
    assert "LIMIT 5" in sql


@pytest.mark.asyncio
async def test_list_meetings_past_direction_caps_explicit_start_at_anchor() -> None:
    session = _CaptureSession()
    context = AgentContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        metadata={"current_datetime": "2026-05-13T12:00:00+00:00"},
    )
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(session))  # type: ignore[method-assign]

    await list_meetings(
        context=context,
        start_datetime=datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
        time_direction="past",
    )

    compiled = session.statements[0].compile(compile_kwargs={"literal_binds": True})
    sql = str(compiled)
    assert "calendar_events.start_at >= '2026-05-01 00:00:00+00:00'" in sql
    assert "calendar_events.start_at < '2026-05-13 12:00:00+00:00'" in sql
    assert "ORDER BY calendar_events.start_at DESC" in sql


@pytest.mark.asyncio
async def test_list_meetings_explicit_bounds_override_temporal_metadata() -> None:
    session = _CaptureSession()
    context = AgentContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        metadata={
            "current_datetime": "2026-05-13T12:00:00+00:00",
            "temporal_direction": "past",
        },
    )
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(session))  # type: ignore[method-assign]

    await list_meetings(
        context=context,
        start_datetime=datetime(2026, 5, 14, 10, 0, tzinfo=UTC),
        end_datetime=datetime(2026, 5, 14, 11, 0, tzinfo=UTC),
    )

    compiled = session.statements[0].compile(compile_kwargs={"literal_binds": True})
    sql = str(compiled)
    assert "calendar_events.start_at < '2026-05-14 11:00:00+00:00'" in sql
    assert "calendar_events.end_at > '2026-05-14 10:00:00+00:00'" in sql
    assert "ORDER BY calendar_events.start_at ASC" in sql
