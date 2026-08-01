"""Contracts excluding instant chat from periodic stale-work cleanup."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from src.workers.chat_worker.services.stale_job_cleanup_service import (
    ChatStaleJobCleanupService,
)


@pytest.mark.asyncio
async def test_periodic_cleanup_does_not_touch_chat_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interactive chat stays owned by its immediate queue and worker lifecycle."""
    service = ChatStaleJobCleanupService(uuid4())
    chat_cleanup = AsyncMock(side_effect=AssertionError("chat cleanup must not run"))

    monkeypatch.setattr(
        service,
        "_recover_stale_attachments",
        AsyncMock(return_value={"requeued": 0, "requeue_failed": 0}),
    )
    monkeypatch.setattr(service, "_cleanup_stale_chat_jobs", chat_cleanup, raising=False)
    monkeypatch.setattr(service, "_cleanup_stuck_reminders", AsyncMock(return_value=0))
    monkeypatch.setattr(service, "_cleanup_stale_reminder_runs", AsyncMock(return_value=0))

    result = await service._run()

    chat_cleanup.assert_not_awaited()
    assert "stale_chat_jobs" not in result
