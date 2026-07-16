"""Contracts for GeneralAgent note, task, and reminder update tools."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from src.agents.context import AgentContext
from src.agents.tools.crm.update_deal_note import update_deal_note
from src.agents.tools.registry import CHANNEL_ONLY_TOOLS, ToolName, ToolRegistry
from src.agents.tools.reminders.update_reminder import update_reminder
from src.agents.tools.tasks.update_task import update_task
from src.domain.models.organisation_tables.reminder_models import (
    ReminderRecurrence,
    ReminderType,
)


class _AsyncSessionContext:
    def __init__(self, session: MagicMock) -> None:
        self.session = session

    async def __aenter__(self) -> MagicMock:
        return self.session

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def _context() -> tuple[AgentContext, MagicMock]:
    session = MagicMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    context = AgentContext(
        organisation_id=UUID("11111111-1111-1111-1111-111111111111"),
        user_id=UUID("22222222-2222-2222-2222-222222222222"),
        channel="whatsapp",
        metadata={
            "user_timezone": "Asia/Calcutta",
            "current_datetime": "2026-07-12T10:00:00+05:30",
        },
    )
    context.acquire_session = MagicMock(return_value=_AsyncSessionContext(session))
    return context, session


@pytest.mark.asyncio
async def test_update_deal_note_commits_only_supplied_fields() -> None:
    """Note updates must preserve omitted fields and commit the shared service write."""
    context, session = _context()
    deal_id, note_id = uuid4(), uuid4()
    note = SimpleNamespace(note_id=note_id, title="New title", body="Existing body")
    service = MagicMock()
    service.update_user_note = AsyncMock(return_value=note)

    with patch("src.agents.tools.crm.update_deal_note.NotesWriteService", return_value=service):
        result = await update_deal_note(
            context=context,
            deal_id=str(deal_id),
            note_id=str(note_id),
            title="New title",
        )

    service.update_user_note.assert_awaited_once_with(
        deal_id=deal_id,
        note_id=note_id,
        payload={"subject": "New title"},
    )
    session.commit.assert_awaited_once()
    assert result == {
        "status": "updated",
        "note_id": str(note_id),
        "deal_id": str(deal_id),
        "title": "New title",
        "body": "Existing body",
        "message": f"Updated note **New title**. Note ID: {note_id}. Deal ID: {deal_id}.",
    }


@pytest.mark.asyncio
async def test_update_task_resolves_assignee_and_due_date_before_committing() -> None:
    """Task updates reuse org-scoped assignee and user-timezone date resolution."""
    context, session = _context()
    task_id = uuid4()
    deal_id = uuid4()
    assignee_id = uuid4()
    task = SimpleNamespace(
        activity_id=task_id,
        deal_id=deal_id,
        related_name="Acme Expansion",
        subject="Send proposal",
        description=None,
        priority="HIGH",
        status="NOT_STARTED",
        due_date=datetime(2026, 7, 13, 11, tzinfo=UTC),
    )
    service = MagicMock()
    service.update_task = AsyncMock(return_value=task)
    service.send_pending_assignment_notifications = AsyncMock()

    with (
        patch("src.agents.tools.tasks.update_task.TaskWriteService", return_value=service),
        patch(
            "src.agents.tools.tasks.update_task._resolve_user_by_name",
            AsyncMock(return_value=(assignee_id, "Alex")),
        ),
        patch(
            "src.agents.tools.tasks.update_task._resolve_due_date",
            return_value=datetime(2026, 7, 13, 11, tzinfo=UTC),
        ),
    ):
        result = await update_task(
            context=context,
            task_id=str(task_id),
            priority="high",
            due_date="tomorrow at 4:30 PM",
            assign_to="Alex",
        )

    service.update_task.assert_awaited_once_with(
        task_id,
        {
            "priority": "HIGH",
            "due_date": datetime(2026, 7, 13, 11, tzinfo=UTC),
            "owner_user_id": assignee_id,
        },
    )
    session.commit.assert_awaited_once()
    service.send_pending_assignment_notifications.assert_awaited_once()
    assert result["status"] == "updated"
    assert result["task_id"] == str(task_id)
    assert result["deal_id"] == str(deal_id)
    assert result["deal_name"] == "Acme Expansion"
    assert str(task_id) in result["message"]
    assert str(deal_id) in result["message"]


@pytest.mark.asyncio
async def test_update_reminder_detects_type_and_maps_agent_prompt() -> None:
    """Reminder updates map content only after reading the owned reminder type."""
    context, _session = _context()
    reminder_id = uuid4()
    deal_id = uuid4()
    existing = SimpleNamespace(reminder_type=ReminderType.AGENTIC)
    detail = SimpleNamespace(
        reminder_id=reminder_id,
        title="Pipeline review",
        reminder_type=ReminderType.AGENTIC,
        deal_id=deal_id,
        next_trigger_at=datetime(2026, 7, 13, 4, 30, tzinfo=UTC),
        recurrence=ReminderRecurrence.DAILY,
    )
    service = MagicMock()
    service.get = AsyncMock(return_value=existing)
    service.update = AsyncMock(return_value=detail)

    with (
        patch("src.agents.tools.reminders.update_reminder.ReminderService", return_value=service),
        patch(
            "src.agents.tools.reminders.update_reminder._resolve_trigger_time",
            return_value=datetime(2026, 7, 13, 4, 30, tzinfo=UTC),
        ),
    ):
        result = await update_reminder(
            context=context,
            reminder_id=str(reminder_id),
            trigger_time="tomorrow at 10 AM",
            agent_prompt="Review every open deal",
            recurrence="daily",
        )

    service.update.assert_awaited_once_with(
        reminder_id,
        {
            "agent_prompt": "Review every open deal",
            "trigger_time": datetime(2026, 7, 13, 4, 30, tzinfo=UTC),
            "recurrence": ReminderRecurrence.DAILY,
        },
        reminder_type=ReminderType.AGENTIC,
    )
    assert result["status"] == "updated"
    assert result["deal_id"] == str(deal_id)
    assert str(reminder_id) in result["message"]
    assert str(deal_id) in result["message"]


def test_update_tools_are_registered_for_general_chat_channels() -> None:
    """General chat must expose every update capability on each supported channel."""
    expected_channels = {"teams", "slack", "whatsapp", "web", "voice"}
    general_tools = ToolRegistry.get_available_tools_for_agent("GeneralAgent")

    for tool_name in (
        ToolName.UPDATE_DEAL_NOTE,
        ToolName.UPDATE_TASK,
        ToolName.UPDATE_REMINDER,
    ):
        assert tool_name in general_tools
        assert CHANNEL_ONLY_TOOLS[tool_name] == expected_channels
