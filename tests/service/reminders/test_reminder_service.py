from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from src.domain.models.master_tables.user_models import UserAppRole
from src.domain.models.organisation_tables.reminder_models import (
    ReminderRecurrence,
    ReminderRunStatus,
    ReminderStatus,
    ReminderType,
    UserReminder,
    UserReminderRun,
)
from src.service.crm.access.record_access_policy import (
    CRMAccessActor,
    CRMRecordAccessError,
)
from src.service.reminders.reminder_service import (
    ReminderNotFoundError,
    ReminderService,
    ReminderServiceError,
)


def _build_reminder(
    *,
    status: ReminderStatus = ReminderStatus.ACTIVE,
    reminder_type: ReminderType = ReminderType.AGENTIC,
    next_trigger_at: datetime | None = None,
) -> UserReminder:
    return UserReminder(
        organisation_id=uuid4(),
        user_id=uuid4(),
        reminder_type=reminder_type,
        title="Weekly deal health check",
        message_content="Follow up with Acme" if reminder_type == ReminderType.STATIC else None,
        agent_config=(
            {"agent_type": "general", "prompt": "Analyze the current state of this deal."}
            if reminder_type == ReminderType.AGENTIC
            else None
        ),
        next_trigger_at=next_trigger_at or datetime(2026, 3, 3, 3, 30, tzinfo=UTC),
        user_timezone="Asia/Kolkata",
        recurrence=ReminderRecurrence.WEEKLY,
        delivery_channel="teams",
        status=status,
    )


def test_to_list_item_includes_schedule_summary_and_action_flags() -> None:
    reminder = _build_reminder()

    item = ReminderService._to_list_item(reminder)

    assert item.next_trigger_local_at == datetime(
        2026, 3, 3, 9, 0, tzinfo=item.next_trigger_local_at.tzinfo
    )
    assert item.schedule_summary == "Weekly on Tuesday at 9:00 AM IST"
    assert item.is_running is False
    assert item.current_run_status is None
    assert item.can_pause is True
    assert item.can_cancel is True
    assert item.can_delete is True


def test_schedule_summary_keeps_next_trigger_out_of_cadence_label() -> None:
    reminder = _build_reminder()

    item = ReminderService._to_list_item(reminder)

    assert "next:" not in item.schedule_summary.lower()
    assert "Mar 03" not in item.schedule_summary


def test_one_time_schedule_summary_does_not_repeat_next_run_timestamp() -> None:
    reminder = _build_reminder()
    reminder.recurrence = ReminderRecurrence.NONE

    item = ReminderService._to_list_item(reminder)

    assert item.schedule_summary == "One-time"


def test_to_detail_marks_triggered_reminders_as_running_and_disables_destructive_actions() -> None:
    reminder = _build_reminder(status=ReminderStatus.TRIGGERED)

    detail = ReminderService._to_detail(reminder)

    assert detail.is_running is True
    assert detail.current_run_status == ReminderRunStatus.IN_PROGRESS
    assert detail.can_pause is False
    assert detail.can_cancel is False
    assert detail.can_delete is False


def test_to_run_item_preserves_input_snapshot_and_result() -> None:
    run = UserReminderRun(
        organisation_id=uuid4(),
        reminder_id=uuid4(),
        user_id=uuid4(),
        attempt_number=4,
        status=ReminderRunStatus.SUCCEEDED,
        triggered_for_at=datetime(2026, 3, 3, 3, 30, tzinfo=UTC),
        input_snapshot="Analyze the current state of this deal.",
        result_text="The deal remains on track.",
        error_message=None,
    )

    item = ReminderService._to_run_item(run)

    assert item.attempt_number == 4
    assert item.status == ReminderRunStatus.SUCCEEDED
    assert item.input_snapshot == "Analyze the current state of this deal."
    assert item.result_text == "The deal remains on track."
    assert item.error_message is None


def test_ensure_not_running_raises_conflict_for_triggered_reminder() -> None:
    reminder = _build_reminder(status=ReminderStatus.TRIGGERED)

    with pytest.raises(ReminderServiceError) as exc_info:
        ReminderService._ensure_not_running(reminder, action="cancel")

    assert exc_info.value.status_code == 409
    assert str(exc_info.value) == "Cannot cancel a reminder while execution is in progress"


class _EmptyResult:
    def scalar_one_or_none(self):
        return None


class _CapturingSession:
    def __init__(self) -> None:
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return _EmptyResult()


@pytest.mark.asyncio
async def test_get_with_expected_type_filters_lookup_by_reminder_type() -> None:
    session = _CapturingSession()
    service = ReminderService(
        session=session,
        organisation_id=uuid4(),
        user_id=uuid4(),
    )

    with pytest.raises(ReminderNotFoundError):
        await service.get(uuid4(), reminder_type=ReminderType.AGENTIC)

    assert session.statement is not None
    assert "user_reminders.reminder_type" in str(session.statement)


def _deal_guarded_service():
    session = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    actor = CRMAccessActor(uuid4(), UserAppRole.MEMBER)
    service = ReminderService(session, uuid4(), actor.user_id)
    service.access_actor = actor
    service._access = MagicMock()
    service._access.require_deal = AsyncMock(
        side_effect=CRMRecordAccessError("Deal not found", 404)
    )
    service._safe_schedule = MagicMock()
    return service, session


@pytest.mark.asyncio
async def test_create_agentic_reminder_rejects_inaccessible_deal() -> None:
    service, session = _deal_guarded_service()

    with pytest.raises(ReminderServiceError, match="Deal not found") as exc_info:
        await service.create(
            title="Deal health check",
            reminder_type=ReminderType.AGENTIC,
            message_content=None,
            agent_prompt="Summarize risks",
            deal_id=uuid4(),
            trigger_time=datetime(2030, 1, 1, tzinfo=UTC),
            timezone="UTC",
            recurrence=ReminderRecurrence.NONE,
            recurrence_end_at=None,
            delivery_channel="teams",
        )

    assert exc_info.value.status_code == 404
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_update_agentic_reminder_rejects_inaccessible_deal() -> None:
    service, session = _deal_guarded_service()
    reminder = _build_reminder()
    service._fetch_owned = AsyncMock(return_value=reminder)

    with pytest.raises(ReminderServiceError, match="Deal not found") as exc_info:
        await service.update(
            reminder.reminder_id,
            {"deal_id": uuid4()},
            reminder_type=ReminderType.AGENTIC,
        )

    assert exc_info.value.status_code == 404
    session.commit.assert_not_awaited()
