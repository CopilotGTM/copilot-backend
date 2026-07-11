"""
Generic Celery task dispatcher with lazy client initialization.

This module provides a worker-neutral boundary for dispatching named Celery tasks
from API and service-layer code without importing worker packages directly.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor
from time import monotonic

from celery import Celery
from src.infra.observability.context import correlation_headers
from src.infra.utils.logger import get_logger

logger = get_logger("task_dispatcher")

_client_lock = threading.Lock()
_task_client: Celery | None = None
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="celery_dispatcher")

DEFAULT_TASK_PUBLISH_TIMEOUT_SECONDS = 3.0


class TaskPublishTimeoutError(TimeoutError):
    """Raised when the broker does not confirm task publication before the API deadline."""


def _create_task_client() -> Celery:
    from src.workers.base.worker_config import BaseWorkerConfig

    config = BaseWorkerConfig()
    client = Celery(
        "task_dispatcher",
        broker=config.broker_url,
        backend=config.backend_url,
    )

    if config.ssl_config:
        client.conf.broker_use_ssl = config.ssl_config
        client.conf.redis_backend_use_ssl = config.ssl_config

    # Durable chat jobs own retries. API producers make one short attempt and use
    # fresh Redis connections so sparse traffic cannot reuse an idle socket.
    client.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        timezone="UTC",
        enable_utc=True,
        broker_connection_retry_on_startup=True,
        broker_connection_retry=False,
        broker_connection_max_retries=0,
        broker_connection_timeout=2,
        broker_pool_limit=0,
        task_publish_retry=False,
        broker_transport_options={
            "socket_timeout": 2,
            "socket_connect_timeout": 2,
            "socket_keepalive": True,
            "retry_on_timeout": False,
        },
        worker_hijack_root_logger=False,
    )

    logger.debug(
        "Task dispatcher initialized (broker=%s)",
        config.broker_url.replace(config.redis_password, "****")
        if config.redis_password
        else config.broker_url,
    )
    return client


def get_task_client() -> Celery:
    """Return the process-local Celery producer client."""
    global _task_client

    if _task_client is None:
        with _client_lock:
            if _task_client is None:
                _task_client = _create_task_client()

    return _task_client


def send_task(
    task_name: str,
    args: list | None = None,
    kwargs: dict | None = None,
    queue: str = "chat_messages",
    routing_key: str | None = None,
    headers: dict | None = None,
) -> str:
    """Publish one task synchronously and return its Celery task ID."""
    client = get_task_client()
    result = client.send_task(
        task_name,
        args=args or [],
        kwargs=kwargs or {},
        queue=queue,
        routing_key=routing_key or queue,
        headers=correlation_headers(headers),
    )
    return str(result.id)


def _record_late_publish_outcome(
    future: asyncio.Future,
    *,
    task_name: str,
    queue: str,
    started_at: float,
) -> None:
    """Log what the broker operation did after the caller's deadline expired."""
    elapsed = monotonic() - started_at
    try:
        task_id = future.result()
    except Exception as exc:
        logger.error(
            "Timed-out Celery publish failed late task=%s queue=%s exception_type=%s "
            "elapsed_seconds=%.3f error=%s",
            task_name,
            queue,
            type(exc).__name__,
            elapsed,
            str(exc) or repr(exc),
            exc_info=(type(exc), exc, exc.__traceback__),
        )
    else:
        logger.warning(
            "Timed-out Celery publish completed late task=%s queue=%s task_id=%s "
            "elapsed_seconds=%.3f",
            task_name,
            queue,
            task_id,
            elapsed,
        )


async def send_task_async(
    task_name: str,
    args: list | None = None,
    kwargs: dict | None = None,
    queue: str = "chat_messages",
    routing_key: str | None = None,
    headers: dict | None = None,
    timeout_seconds: float = DEFAULT_TASK_PUBLISH_TIMEOUT_SECONDS,
) -> str:
    """Publish a task without allowing broker stalls to pin an API request."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    started_at = monotonic()
    publish_future = loop.run_in_executor(
        _executor,
        lambda: ctx.run(send_task, task_name, args, kwargs, queue, routing_key, headers),
    )
    logger.debug(
        "Publishing Celery task task=%s queue=%s timeout_seconds=%.3f",
        task_name,
        queue,
        timeout_seconds,
    )

    try:
        task_id = await asyncio.wait_for(
            asyncio.shield(publish_future),
            timeout=timeout_seconds,
        )
    except TimeoutError as exc:
        elapsed = monotonic() - started_at
        logger.error(
            "Celery task publish timed out task=%s queue=%s timeout_seconds=%.2f "
            "elapsed_seconds=%.3f",
            task_name,
            queue,
            timeout_seconds,
            elapsed,
        )
        publish_future.add_done_callback(
            lambda completed: _record_late_publish_outcome(
                completed,
                task_name=task_name,
                queue=queue,
                started_at=started_at,
            )
        )
        raise TaskPublishTimeoutError(
            f"Celery publish timed out: task={task_name} queue={queue} "
            f"timeout_seconds={timeout_seconds:.2f}"
        ) from exc
    except Exception as exc:
        elapsed = monotonic() - started_at
        logger.exception(
            "Celery task publish failed task=%s queue=%s exception_type=%s "
            "elapsed_seconds=%.3f error=%s",
            task_name,
            queue,
            type(exc).__name__,
            elapsed,
            str(exc) or repr(exc),
        )
        raise

    logger.info(
        "Celery task published task=%s queue=%s task_id=%s elapsed_seconds=%.3f",
        task_name,
        queue,
        task_id,
        monotonic() - started_at,
    )
    return task_id


__all__ = [
    "DEFAULT_TASK_PUBLISH_TIMEOUT_SECONDS",
    "TaskPublishTimeoutError",
    "get_task_client",
    "send_task",
    "send_task_async",
]
