from __future__ import annotations

from uuid import UUID, uuid4

from src.workers.chat_worker.tasks import reminder_tasks


def test_poll_due_reminders_uses_larger_batch_and_requeues_dispatch_failures(
    monkeypatch,
) -> None:
    org_id = str(uuid4())
    reminder_id = str(uuid4())
    calls: dict[str, object] = {}

    def fake_pop_due_reminders(now_epoch: float, limit: int):
        calls["limit"] = limit
        return [(org_id, reminder_id)]

    def fake_schedule_reminder(
        rescheduled_org_id: str,
        rescheduled_reminder_id: str,
        trigger_epoch: float,
    ) -> None:
        calls["rescheduled"] = (
            rescheduled_org_id,
            rescheduled_reminder_id,
            trigger_epoch,
        )

    def fail_dispatch(**_kwargs) -> None:
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(
        "src.infra.messaging.reminder_schedule.pop_due_reminders",
        fake_pop_due_reminders,
    )
    monkeypatch.setattr(
        "src.infra.messaging.reminder_schedule.schedule_reminder",
        fake_schedule_reminder,
    )
    monkeypatch.setattr(
        reminder_tasks.execute_reminder_task,
        "apply_async",
        fail_dispatch,
    )
    monkeypatch.setattr(reminder_tasks.time, "time", lambda: 123.0)

    result = reminder_tasks.poll_due_reminders_task()

    assert calls["limit"] == 500
    assert calls["rescheduled"] == (UUID(org_id), UUID(reminder_id), 123.0)
    assert result == {
        "status": "dispatched",
        "claimed": 1,
        "dispatched": 0,
        "requeued": 1,
    }
