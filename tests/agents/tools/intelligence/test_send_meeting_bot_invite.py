"""Regression tests for sending meeting bots from agent tools.

The tool has a real external side effect, so these tests lock two contracts:
the agent must confirm inferred "next meeting" sends, and every dispatch must
first prove the event belongs to the current user's connected calendar.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from src.agents.context import AgentContext
from src.agents.tools.intelligence.list_meetings_unified import (
    CALENDAR_INTEGRATION_TYPES as LIST_MEETINGS_CALENDAR_INTEGRATION_TYPES,
)
from src.agents.tools.intelligence.send_meeting_bot_invite import (
    CALENDAR_INTEGRATION_TYPES,
    send_meeting_bot_invite,
)
from src.service.integrations.calendar.integration_types import (
    PROVIDER_PARENT_CALENDAR_INTEGRATION_TYPE_VALUES,
)


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

    def first(self) -> Any:
        """Mimic SQLAlchemy's scalar first() lookup used by the tool."""
        return self.value


class _ExecuteResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalars(self) -> _ScalarResult:
        """Return the scalar facade expected by SQLModel select queries."""
        return _ScalarResult(self.value)


class _CaptureSession:
    def __init__(self, event: Any) -> None:
        self.event = event
        self.statements: list[Any] = []

    async def execute(self, stmt: Any) -> _ExecuteResult:
        """Capture the generated statement so tests can inspect ownership joins."""
        self.statements.append(stmt)
        return _ExecuteResult(self.event)


def _context(session: _CaptureSession, *, user_id: UUID | None = None) -> AgentContext:
    context = AgentContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=user_id or UUID("22222222-2222-2222-2222-222222222222"),
        metadata={"current_datetime": "2026-05-13T12:00:00+00:00"},
    )
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(session))  # type: ignore[method-assign]
    return context


def _event() -> SimpleNamespace:
    return SimpleNamespace(
        event_id=uuid4(),
        title="Customer sync",
        start_at=datetime.now(UTC) + timedelta(hours=2),
        end_at=datetime.now(UTC) + timedelta(hours=3),
        conference_link="https://meet.google.com/abc-defg-hij",
    )


def test_calendar_tool_filters_provider_parent_integrations() -> None:
    """Meeting-bot ownership checks should use provider rows, not sync capabilities."""
    assert CALENDAR_INTEGRATION_TYPES == PROVIDER_PARENT_CALENDAR_INTEGRATION_TYPE_VALUES
    assert LIST_MEETINGS_CALENDAR_INTEGRATION_TYPES == (
        PROVIDER_PARENT_CALENDAR_INTEGRATION_TYPE_VALUES
    )


@pytest.mark.asyncio
async def test_next_meeting_requires_confirmation_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inferred next-meeting sends return a preview until the user confirms."""
    event = _event()
    session = _CaptureSession(event)
    dispatch = AsyncMock()
    monkeypatch.setattr(
        "src.agents.tools.intelligence.send_meeting_bot_invite.MeetingRecordingDispatchService",
        MagicMock(return_value=SimpleNamespace(dispatch_event=dispatch)),
    )

    result = await send_meeting_bot_invite(context=_context(session), confirmed=False)

    assert result["status"] == "confirmation_required"
    assert result["event"]["event_id"] == str(event.event_id)
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_specific_event_is_verified_against_user_calendar_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit event sends still require the event to belong to this user's calendar."""
    event = _event()
    session = _CaptureSession(event)
    dispatch = AsyncMock(
        return_value={"status": "already_sent", "calendar_event_id": str(event.event_id)}
    )
    monkeypatch.setattr(
        "src.agents.tools.intelligence.send_meeting_bot_invite.MeetingRecordingDispatchService",
        MagicMock(return_value=SimpleNamespace(dispatch_event=dispatch)),
    )

    result = await send_meeting_bot_invite(
        context=_context(session),
        event_id=str(event.event_id),
    )

    assert result["status"] == "already_sent"
    dispatch.assert_awaited_once_with(event.event_id)
    sql = str(session.statements[0])
    assert "knowledge_integrations.created_by_user_id" in sql
    assert "calendar_event_user_states.is_deleted" in sql


@pytest.mark.asyncio
async def test_missing_user_id_never_dispatches_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tool must not fall back to org-wide event lookup without user context."""
    session = _CaptureSession(_event())
    context = _context(session, user_id=UUID("22222222-2222-2222-2222-222222222222"))
    context.user_id = None
    dispatch = AsyncMock()
    monkeypatch.setattr(
        "src.agents.tools.intelligence.send_meeting_bot_invite.MeetingRecordingDispatchService",
        MagicMock(return_value=SimpleNamespace(dispatch_event=dispatch)),
    )

    result = await send_meeting_bot_invite(context=context, confirmed=True)

    assert result == {"status": "error", "reason": "missing_user_context"}
    context.acquire_session.assert_not_called()
    dispatch.assert_not_awaited()
