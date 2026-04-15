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

from celery import Celery
from src.infra.observability.context import correlation_headers
from src.infra.utils.logger import get_logger

logger = get_logger("task_dispatcher")

_client_lock = threading.Lock()
_task_client: Celery | None = None
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="celery_dispatcher")


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

    client.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        timezone="UTC",
        enable_utc=True,
        broker_connection_retry_on_startup=True,
        broker_connection_retry=True,
        broker_connection_max_retries=5,
        broker_connection_timeout=10,
        broker_transport_options={
            "socket_timeout": 10,
            "socket_connect_timeout": 10,
            "socket_keepalive": True,
            "retry_on_timeout": True,
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


async def send_task_async(
    task_name: str,
    args: list | None = None,
    kwargs: dict | None = None,
    queue: str = "chat_messages",
    routing_key: str | None = None,
    headers: dict | None = None,
) -> str:
    loop = asyncio.get_event_loop()
    ctx = contextvars.copy_context()
    return await loop.run_in_executor(
        _executor,
        lambda: ctx.run(send_task, task_name, args, kwargs, queue, routing_key, headers),
    )


__all__ = [
    "get_task_client",
    "send_task",
    "send_task_async",
]
