from datetime import UTC, datetime

from src.domain.models.organisation_tables.reminder_models import ReminderRecurrence
from src.workers.chat_worker.services.reminder_execution_service import ReminderExecutionService


def test_compute_next_trigger_monthly_clamps_to_last_day_of_shorter_month() -> None:
    current_trigger = datetime(2026, 1, 31, 10, 0, tzinfo=UTC)

    next_trigger = ReminderExecutionService._compute_next_trigger(
        current_trigger=current_trigger,
        recurrence=ReminderRecurrence.MONTHLY,
        user_timezone="UTC",
    )

    assert next_trigger == datetime(2026, 2, 28, 10, 0, tzinfo=UTC)


def test_compute_next_trigger_monthly_preserves_local_time_in_user_timezone() -> None:
    current_trigger = datetime(2026, 3, 31, 3, 30, tzinfo=UTC)

    next_trigger = ReminderExecutionService._compute_next_trigger(
        current_trigger=current_trigger,
        recurrence=ReminderRecurrence.MONTHLY,
        user_timezone="Asia/Kolkata",
    )

    assert next_trigger == datetime(2026, 4, 30, 3, 30, tzinfo=UTC)


def test_compute_next_trigger_monthly_falls_back_to_utc_for_invalid_timezone() -> None:
    current_trigger = datetime(2026, 1, 31, 10, 0, tzinfo=UTC)

    next_trigger = ReminderExecutionService._compute_next_trigger(
        current_trigger=current_trigger,
        recurrence=ReminderRecurrence.MONTHLY,
        user_timezone="Not/ARealTimezone",
    )

    assert next_trigger == datetime(2026, 2, 28, 10, 0, tzinfo=UTC)
