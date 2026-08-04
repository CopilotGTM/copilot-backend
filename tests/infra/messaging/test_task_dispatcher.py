"""Regression tests for bounded, observable Celery task publication."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest
from celery import Celery
from kombu import pools
from src.infra.messaging import task_dispatcher

PUBLISH_CONTRACTS = [
    ("chat.process_message", "chat_messages"),
    ("chat.process_bot_message", "chat_messages"),
]


def test_task_client_reuses_health_checked_redis_connections(monkeypatch):
    """API publication should reuse TLS connections without trusting stale sockets."""

    class _Config:
        broker_url = "redis://localhost:6379/0"
        backend_url = broker_url
        redis_password = ""
        ssl_config = None

    from src.workers.base import worker_config

    monkeypatch.setattr(worker_config, "BaseWorkerConfig", _Config)

    client = task_dispatcher._create_task_client()

    assert client.conf.broker_pool_limit == 10
    assert client.conf.broker_transport_options["health_check_interval"] == 30
    assert client.conf.task_publish_retry is False


def test_task_client_does_not_shrink_an_in_use_default_producer_pool(monkeypatch):
    """Mixed API producers must share Kombu's process-wide limit of ten."""

    class _Config:
        broker_url = "memory://"
        backend_url = "cache+memory://"
        redis_password = ""
        ssl_config = None

    from src.workers.base import worker_config

    monkeypatch.setattr(worker_config, "BaseWorkerConfig", _Config)
    original_limit = pools.get_limit()
    pools.reset()
    pools.set_limit(10)
    existing_client = Celery("existing_api_producer", broker="memory://")
    borrowed_connections = [existing_client.pool.acquire() for _ in range(5)]

    try:
        dispatcher_client = task_dispatcher._create_task_client()

        # Accessing .pool applies this app's configured limit to every Kombu
        # pool. The old value of four raised while these five were borrowed.
        assert dispatcher_client.pool.limit == 10
    finally:
        for connection in borrowed_connections:
            connection.release()
        pools.reset()
        pools.set_limit(original_limit)


def test_send_task_bypasses_the_process_global_producer_pool(monkeypatch):
    """Each publish owns its connection so an idle pooled producer cannot block it."""
    original_limit = pools.get_limit()
    pools.reset()
    pools.set_limit(10)
    client = Celery("isolated_dispatch", broker="memory://", backend="cache+memory://")
    producer_pool = client.producer_pool
    acquire = MagicMock(wraps=producer_pool.acquire)
    monkeypatch.setattr(producer_pool, "acquire", acquire)
    monkeypatch.setattr(task_dispatcher, "get_task_client", lambda: client)

    try:
        task_id = task_dispatcher.send_task(
            "chat.process_bot_message",
            args=["org-id", "job-id"],
            queue="chat_messages",
        )

        assert task_id
        acquire.assert_not_called()
    finally:
        pools.reset()
        pools.set_limit(original_limit)


def _assert_publish_event(call, *, task_name: str, queue: str, outcome: str) -> None:
    extra = call.kwargs["extra"]
    assert extra["event"] == "celery_publish"
    assert extra["task_name"] == task_name
    assert extra["queue"] == queue
    assert extra["outcome"] == outcome
    assert extra["duration_ms"] >= 0
    assert extra["publish_attempt_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("task_name", "queue"), PUBLISH_CONTRACTS)
async def test_publish_timeout_is_descriptive_and_records_late_completion(
    monkeypatch,
    task_name,
    queue,
):
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
        match=rf"{task_name}.*{queue}.*0\.01",
    ):
        await task_dispatcher.send_task_async(
            task_name,
            args=["org-id", "job-id"],
            queue=queue,
            timeout_seconds=0.01,
        )

    release.set()
    await asyncio.sleep(0.05)

    timeout_log = logger.error.call_args_list[0]
    assert timeout_log.args[0].startswith("Celery task publish timed out")
    assert timeout_log.args[1:4] == (task_name, queue, 0.01)
    _assert_publish_event(timeout_log, task_name=task_name, queue=queue, outcome="timeout")
    late_completion_log = next(
        call
        for call in logger.warning.call_args_list
        if call.args[0].startswith("Timed-out Celery publish completed late")
    )
    _assert_publish_event(
        late_completion_log,
        task_name=task_name,
        queue=queue,
        outcome="completed_late",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("task_name", "queue"), PUBLISH_CONTRACTS)
async def test_publish_failure_logs_exception_type_and_elapsed_time(
    monkeypatch,
    task_name,
    queue,
):
    """A broker exception must retain its concrete type in operational logs."""
    logger = MagicMock()

    def _failed_send(*_args, **_kwargs):
        raise ConnectionError("broker connection closed")

    monkeypatch.setattr(task_dispatcher, "send_task", _failed_send)
    monkeypatch.setattr(task_dispatcher, "logger", logger)

    with pytest.raises(ConnectionError, match="broker connection closed"):
        await task_dispatcher.send_task_async(
            task_name,
            queue=queue,
            timeout_seconds=0.5,
        )

    failure_log = logger.exception.call_args
    assert failure_log.args[0].startswith("Celery task publish failed")
    _assert_publish_event(failure_log, task_name=task_name, queue=queue, outcome="failed")


@pytest.mark.asyncio
@pytest.mark.parametrize(("task_name", "queue"), PUBLISH_CONTRACTS)
async def test_publish_success_records_structured_outcome(monkeypatch, task_name, queue):
    """A successful publish must emit the same queryable event contract."""
    logger = MagicMock()
    published_headers = None

    def _successful_send(
        _task_name,
        _args,
        _kwargs,
        _queue,
        _routing_key,
        headers,
    ):
        nonlocal published_headers
        published_headers = headers
        return "task-id"

    monkeypatch.setattr(task_dispatcher, "send_task", _successful_send)
    monkeypatch.setattr(task_dispatcher, "logger", logger)

    task_id = await task_dispatcher.send_task_async(
        task_name,
        queue=queue,
        headers={"request_id": "request-123"},
        timeout_seconds=0.5,
    )

    assert task_id == "task-id"
    assert published_headers == {"request_id": "request-123"}
    _assert_publish_event(
        logger.info.call_args,
        task_name=task_name,
        queue=queue,
        outcome="published",
    )


@pytest.mark.asyncio
async def test_timed_out_publishes_do_not_trap_the_next_healthy_publish(monkeypatch):
    """Timed-out broker calls must not permanently consume publisher capacity."""
    worker_count = 4
    executor = ThreadPoolExecutor(max_workers=worker_count)
    release = threading.Event()
    started = 0
    started_lock = threading.Lock()
    replacement_executor = None

    def _blocked_send(*_args, **_kwargs):
        nonlocal started
        with started_lock:
            started += 1
        release.wait()
        return "late-task-id"

    monkeypatch.setattr(task_dispatcher, "_executor", executor)
    monkeypatch.setattr(task_dispatcher, "_executor_generation", 0)
    monkeypatch.setattr(task_dispatcher, "_generation_inflight", {})
    monkeypatch.setattr(task_dispatcher, "_retired_generations", set())
    monkeypatch.setattr(task_dispatcher, "send_task", _blocked_send)

    blocked_publishes = [
        asyncio.create_task(
            task_dispatcher.send_task_async(
                "chat.process_message",
                queue="chat_messages",
                timeout_seconds=0.05,
            )
        )
        for _ in range(worker_count)
    ]

    try:
        while True:
            with started_lock:
                if started == worker_count:
                    break
            await asyncio.sleep(0.001)

        results = await asyncio.gather(*blocked_publishes, return_exceptions=True)
        assert all(
            isinstance(result, task_dispatcher.TaskPublishTimeoutError) for result in results
        )
        replacement_executor = task_dispatcher._executor

        monkeypatch.setattr(
            task_dispatcher,
            "send_task",
            lambda *_args, **_kwargs: "healthy-task-id",
        )
        task_id = await task_dispatcher.send_task_async(
            "chat.process_message",
            queue="chat_messages",
            timeout_seconds=0.05,
        )

        assert task_id == "healthy-task-id"
    finally:
        release.set()
        await asyncio.gather(*blocked_publishes, return_exceptions=True)
        executor.shutdown(wait=True)
        if replacement_executor is not None:
            replacement_executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_sustained_timeouts_keep_executor_recovery_bounded(monkeypatch):
    """A broker outage must not create unbounded generations of blocked threads."""
    worker_count = 4
    executor = ThreadPoolExecutor(max_workers=worker_count)
    release = threading.Event()

    def _blocked_send(*_args, **_kwargs):
        release.wait()
        return "late-task-id"

    monkeypatch.setattr(task_dispatcher, "_executor", executor)
    monkeypatch.setattr(task_dispatcher, "_executor_generation", 0)
    monkeypatch.setattr(task_dispatcher, "_generation_inflight", {})
    monkeypatch.setattr(task_dispatcher, "_retired_generations", set())
    monkeypatch.setattr(task_dispatcher, "send_task", _blocked_send)

    first_wave = [
        asyncio.create_task(
            task_dispatcher.send_task_async(
                "chat.process_message",
                queue="chat_messages",
                timeout_seconds=0.05,
            )
        )
        for _ in range(worker_count)
    ]
    second_wave = []
    replacement_executor = None
    try:
        first_results = await asyncio.gather(*first_wave, return_exceptions=True)
        assert all(
            isinstance(result, task_dispatcher.TaskPublishTimeoutError) for result in first_results
        )
        replacement_executor = task_dispatcher._executor
        assert task_dispatcher._executor_generation == 1

        second_wave = [
            asyncio.create_task(
                task_dispatcher.send_task_async(
                    "chat.process_message",
                    queue="chat_messages",
                    timeout_seconds=0.05,
                )
            )
            for _ in range(worker_count)
        ]
        second_results = await asyncio.gather(*second_wave, return_exceptions=True)

        assert all(
            isinstance(result, task_dispatcher.TaskPublishTimeoutError) for result in second_results
        )
        assert task_dispatcher._executor is replacement_executor
        assert task_dispatcher._executor_generation == 1
    finally:
        release.set()
        await asyncio.gather(*first_wave, *second_wave, return_exceptions=True)
        executor.shutdown(wait=True)
        if replacement_executor is not None:
            replacement_executor.shutdown(wait=True)
