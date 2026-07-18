"""Regression coverage for task-linked reminder delivery fallback."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from src.service.crm.tasks.reminder_service import TaskReminderService


@pytest.mark.asyncio
async def test_cancel_task_reminders_includes_in_flight_delivery() -> None:
    """Completing a task cancels a reminder that has already been claimed."""
    organisation_id = uuid4()
    activity_id = uuid4()
    reminder = type(
        "Reminder",
        (),
        {"reminder_id": uuid4(), "status": "TRIGGERED"},
    )()

    class ScalarResult:
        """Return the in-flight reminder selected for cancellation."""

        def all(self):
            """Expose the selected reminder list."""
            return [reminder]

    class ExecuteResult:
        """Wrap the scalar reminder result."""

        def scalars(self):
            """Return the scalar query result."""
            return ScalarResult()

    class FakeSession:
        """Capture cancellation SQL and task reminder cleanup."""

        def __init__(self):
            """Initialize captured statement state."""
            self.statement = None

        async def execute(self, statement):
            """Capture the status-filtered cancellation query."""
            self.statement = statement
            return ExecuteResult()

        async def get(self, _model, _activity_id):
            """Return a task carrying the denormalized reminder timestamp."""
            return type("Task", (), {"reminder_at": object()})()

        async def flush(self):
            """Accept the cancellation flush."""
            return None

    session = FakeSession()
    service = TaskReminderService(session, organisation_id, uuid4())  # type: ignore[arg-type]
    service.safe_unschedule = Mock()  # type: ignore[method-assign]

    await service.cancel_task_reminders(activity_id)

    assert "triggered" in session.statement.compile().params["status_1"]
    assert reminder.status.value == "cancelled"


@pytest.mark.asyncio
async def test_task_reminder_falls_back_from_teams_to_slack_to_whatsapp() -> None:
    """The first connected owner channel wins, with WhatsApp as the final fallback."""
    service = TaskReminderService(object(), uuid4(), uuid4())  # type: ignore[arg-type]
    whatsapp_target = ("whatsapp", {"phone_number_id": "phone", "recipient_wa_id": "user"})
    resolver = AsyncMock(side_effect=[None, None, whatsapp_target])
    service.resolve_delivery_target_for_channel = resolver  # type: ignore[method-assign]

    resolved = await service.resolve_task_reminder_target(None)

    assert resolved == whatsapp_target
    assert [call.args[0] for call in resolver.await_args_list] == [
        "teams",
        "slack",
        "whatsapp",
    ]
