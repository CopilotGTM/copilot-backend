from __future__ import annotations

from src.workers.chat_worker.config import ChatWorkerConfig


def test_stale_chat_job_cleanup_is_not_scheduled_by_beat() -> None:
    config = ChatWorkerConfig()

    assert "cleanup-stale-chat-jobs" not in config.beat_schedule
    assert "chat.cleanup_stale_jobs_all_orgs" in config.task_routes
    assert "chat.cleanup_stale_jobs_org" in config.task_routes
