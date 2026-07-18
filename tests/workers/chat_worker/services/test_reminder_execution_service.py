"""Regression coverage for reminder recurrence and task-lifecycle suppression."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.reminder_models import (
    ReminderRecurrence,
    ReminderRunStatus,
    ReminderStatus,
)
from src.domain.models.organisation_tables.sync.crm import CRMActivityStatus
from src.workers.chat_worker.services.reminder_execution_service import ReminderExecutionService


@pytest.mark.asyncio
async def test_load_and_lock_cancels_reminder_for_completed_task(monkeypatch) -> None:
    """A stale schedule entry must not notify after its linked task is completed."""
    organisation_id = uuid4()
    task_id = uuid4()
    reminder = SimpleNamespace(
        reminder_id=uuid4(),
        organisation_id=organisation_id,
        activity_id=task_id,
        status=ReminderStatus.ACTIVE,
    )
    task = SimpleNamespace(
        task_id=task_id,
        status=CRMActivityStatus.COMPLETED.value,
        is_deleted=False,
    )

    class ScalarResult:
        """Return the active reminder selected by the execution lock query."""

        def first(self):
            """Expose the selected reminder through SQLAlchemy's scalar API."""
            return reminder

    class ExecuteResult:
        """Wrap the scalar result used by the reminder loader."""

        def scalars(self):
            """Return the scalar reminder result."""
            return ScalarResult()

    class FakeSession:
        """Model the transaction that cancels a stale scheduled reminder."""

        committed = False

        async def execute(self, _statement):
            """Return the active reminder for the lock query."""
            return ExecuteResult()

        async def get(self, _model, _entity_id):
            """Return the completed linked task."""
            return task

        def add(self, _row):
            """Accept the cancelled reminder update."""
            return None

        async def commit(self):
            """Record the durable cancellation."""
            self.committed = True

    session = FakeSession()

    async def fake_acquire_session(_organisation_id):
        """Yield the task/reminder transaction without a tenant database."""
        yield session

    monkeypatch.setattr(
        "src.workers.chat_worker.services.reminder_execution_service.acquire_org_async_session",
        fake_acquire_session,
    )

    result = await ReminderExecutionService(organisation_id)._load_and_lock(reminder.reminder_id)

    assert result is None
    assert reminder.status == ReminderStatus.CANCELLED
    assert session.committed is True


@pytest.mark.asyncio
async def test_static_whatsapp_task_reminder_uses_approved_template(monkeypatch) -> None:
    """Scheduled WhatsApp task reminders must work outside the free-text service window."""
    sent_payloads: list[dict] = []

    class FakeWhatsAppProvider:
        """Capture the approved-template call without contacting Meta."""

        async def send_template_message_with_id(self, **kwargs):
            """Record template parameters and model a provider message ID."""
            sent_payloads.append(kwargs)
            return "wamid-1"

    monkeypatch.setattr(
        "src.service.channels.WhatsAppOutboundProvider",
        FakeWhatsAppProvider,
    )
    service = ReminderExecutionService(uuid4())
    service._send_text = AsyncMock()  # type: ignore[method-assign]
    task_id = uuid4()

    success, _ = await service._execute_static(
        {
            "delivery_channel": "whatsapp",
            "delivery_context": {
                "phone_number_id": "phone-1",
                "recipient_wa_id": "user-1",
            },
            "activity_id": task_id,
            "title": "Send proposal",
            "message_content": "Reminder for task",
            "task_title": "Send proposal",
            "task_due_at": datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
            "task_due_date": None,
            "deal_name": "Acme",
        }
    )

    assert success is True
    assert sent_payloads[0]["template_name"] == "task_reminder"
    assert sent_payloads[0]["body_parameters"][0:3] == [
        "Send proposal",
        "July 20, 2026 at 10:00 AM",
        "Acme",
    ]
    assert str(task_id) in sent_payloads[0]["body_parameters"][3]
    service._send_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery_succeeds", [True, False])
async def test_in_flight_delivery_does_not_revive_cancelled_task_reminder(
    monkeypatch,
    delivery_succeeds,
) -> None:
    """Completion cancellation wins if provider delivery finishes concurrently."""
    organisation_id = uuid4()
    reminder_id = uuid4()
    run_id = uuid4()
    reminder = SimpleNamespace(
        reminder_id=reminder_id,
        organisation_id=organisation_id,
        status=ReminderStatus.CANCELLED,
        last_triggered_at=None,
        trigger_count=0,
        consecutive_failures=0,
        last_error=None,
        next_trigger_at=None,
    )
    run = SimpleNamespace(
        status=ReminderRunStatus.IN_PROGRESS,
        completed_at=None,
        result_text=None,
        error_message=None,
    )

    class Result:
        """Expose one predetermined row through SQLAlchemy's scalar API."""

        def __init__(self, row):
            """Store the row returned by this query."""
            self.row = row

        def scalars(self):
            """Return this result as its scalar wrapper."""
            return self

        def first(self):
            """Return the selected reminder or run."""
            return self.row

    class FakeSession:
        """Return the reminder then its run from the two lifecycle queries."""

        def __init__(self):
            """Start before either query has executed."""
            self.calls = 0

        async def execute(self, _statement):
            """Return rows in the same order as the lifecycle method queries them."""
            self.calls += 1
            return Result(reminder if self.calls == 1 else run)

        def add(self, _row):
            """Accept lifecycle updates."""
            return None

        async def commit(self):
            """Accept the durable lifecycle commit."""
            return None

    async def fake_org_session(_organisation_id):
        """Yield one transaction for the lifecycle update."""
        yield FakeSession()

    monkeypatch.setattr(
        "src.workers.chat_worker.services.reminder_execution_service.acquire_org_async_session",
        fake_org_session,
    )
    service = ReminderExecutionService(organisation_id)

    if delivery_succeeds:
        await service._mark_success(
            reminder_id,
            run_id,
            {
                "trigger_count": 0,
                "recurrence": ReminderRecurrence.NONE,
                "user_timezone": "UTC",
            },
            "sent",
        )
    else:
        await service._mark_failure(
            reminder_id,
            run_id,
            "provider unavailable",
            run_status=ReminderRunStatus.DELIVERY_FAILED,
        )

    assert reminder.status == ReminderStatus.CANCELLED


def test_compute_next_trigger_monthly_clamps_to_last_day_of_shorter_month() -> None:
    """Monthly recurrence must clamp January 31 to February's final day."""
    current_trigger = datetime(2026, 1, 31, 10, 0, tzinfo=UTC)

    next_trigger = ReminderExecutionService._compute_next_trigger(
        current_trigger=current_trigger,
        recurrence=ReminderRecurrence.MONTHLY,
        user_timezone="UTC",
    )

    assert next_trigger == datetime(2026, 2, 28, 10, 0, tzinfo=UTC)


def test_compute_next_trigger_monthly_preserves_local_time_in_user_timezone() -> None:
    """Monthly recurrence keeps the user's wall-clock time across month lengths."""
    current_trigger = datetime(2026, 3, 31, 3, 30, tzinfo=UTC)

    next_trigger = ReminderExecutionService._compute_next_trigger(
        current_trigger=current_trigger,
        recurrence=ReminderRecurrence.MONTHLY,
        user_timezone="Asia/Kolkata",
    )

    assert next_trigger == datetime(2026, 4, 30, 3, 30, tzinfo=UTC)


def test_compute_next_trigger_monthly_falls_back_to_utc_for_invalid_timezone() -> None:
    """Invalid stored timezones use UTC rather than failing reminder execution."""
    current_trigger = datetime(2026, 1, 31, 10, 0, tzinfo=UTC)

    next_trigger = ReminderExecutionService._compute_next_trigger(
        current_trigger=current_trigger,
        recurrence=ReminderRecurrence.MONTHLY,
        user_timezone="Not/ARealTimezone",
    )

    assert next_trigger == datetime(2026, 2, 28, 10, 0, tzinfo=UTC)
