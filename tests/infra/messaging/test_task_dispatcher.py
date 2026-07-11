"""Regression tests for bounded, observable Celery task publication."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock

import pytest
from src.infra.messaging import task_dispatcher


@pytest.mark.asyncio
async def test_publish_timeout_is_descriptive_and_records_late_completion(monkeypatch):
    """A blocked producer must fail quickly without hiding its eventual outcome."""
    release = threading.Event()
    logger = MagicMock()

    def _delayed_send(*_args, **_kwargs):
        release.wait(timeout=1)
        return "late-task-id"

    monkeypatch.setattr(task_dispatcher, "send_task", _delayed_send)
    monkeypatch.setattr(task_dispatcher, "logger", logger)

    with pytest.raises(
        task_dispatcher.TaskPublishTimeoutError,
        match=r"chat\.process_message.*chat_messages.*0\.01",
    ):
        await task_dispatcher.send_task_async(
            "chat.process_message",
            args=["org-id", "job-id"],
            queue="chat_messages",
            timeout_seconds=0.01,
        )

    release.set()
    await asyncio.sleep(0.05)

    timeout_log = logger.error.call_args_list[0]
    assert timeout_log.args[0].startswith("Celery task publish timed out")
    assert timeout_log.args[1:4] == ("chat.process_message", "chat_messages", 0.01)
    assert any(
        call.args[0].startswith("Timed-out Celery publish completed late")
        for call in logger.warning.call_args_list
    )


@pytest.mark.asyncio
async def test_publish_failure_logs_exception_type_and_elapsed_time(monkeypatch):
    """A broker exception must retain its concrete type in operational logs."""
    logger = MagicMock()

    def _failed_send(*_args, **_kwargs):
        raise ConnectionError("broker connection closed")

    monkeypatch.setattr(task_dispatcher, "send_task", _failed_send)
    monkeypatch.setattr(task_dispatcher, "logger", logger)

    with pytest.raises(ConnectionError, match="broker connection closed"):
        await task_dispatcher.send_task_async(
            "chat.process_message",
            queue="chat_messages",
            timeout_seconds=0.5,
        )

    failure_log = logger.exception.call_args
    assert failure_log.args[0].startswith("Celery task publish failed")
