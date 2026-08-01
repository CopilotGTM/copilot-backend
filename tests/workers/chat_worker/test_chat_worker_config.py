"""Beat-schedule contracts for chat recovery and reminder polling."""

from __future__ import annotations

from src.workers.chat_worker.config import ChatWorkerConfig


def test_remaining_stale_job_cleanup_runs_once_per_minute() -> None:
    config = ChatWorkerConfig()

    schedule = config.beat_schedule["cleanup-stale-chat-jobs"]
    assert schedule["task"] == "chat.cleanup_stale_jobs_all_orgs"
    assert schedule["schedule"] == 60.0
    assert schedule["options"] == {
        "queue": "chat_processing",
        "routing_key": "chat_processing",
    }
    assert "chat.cleanup_stale_jobs_all_orgs" in config.task_routes
    assert "chat.cleanup_stale_jobs_org" in config.task_routes


def test_reminder_recovery_sweep_is_not_scheduled_by_beat() -> None:
    config = ChatWorkerConfig()

    assert "recovery-sweep-reminders" not in config.beat_schedule
    assert "chat.recovery_sweep_reminders" in config.task_routes


def test_due_reminder_poll_runs_once_per_minute() -> None:
    config = ChatWorkerConfig()

    assert config.beat_schedule["poll-due-reminders"]["schedule"] == 60.0
