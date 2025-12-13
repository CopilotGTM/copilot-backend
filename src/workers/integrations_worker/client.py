"""
Lightweight Celery client for sending tasks to the integrations worker.

This module provides a minimal Celery client that can be used by the FastAPI
application to enqueue tasks without loading the full worker configuration
and signal handlers. This avoids the overhead of initializing the full Celery
app at import time, which can cause blocking issues during FastAPI startup.

IMPORTANT: The send_task() call in Celery is synchronous and can block when
connecting to Redis (especially with SSL). This module provides async wrappers
that run send_task in a thread pool to avoid blocking the asyncio event loop.

Usage:
    from src.workers.integrations_worker.client import send_integrations_task_async

    task_id = await send_integrations_task_async(
        "integrations.sync_integration",
        args=[org_id, integration_id],
        queue="integrations"
    )
"""

import asyncio
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor

from celery import Celery

from src.infra.observability.context import correlation_headers
from src.infra.utils.logger import get_logger

logger = get_logger("integrations_worker_client")

# Thread-safe lazy initialization
_client_lock = threading.Lock()
_integrations_client: Celery | None = None

# Thread pool for running blocking Celery operations
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="celery_integrations_client")


def _create_integrations_client() -> Celery:
    """
    Create a minimal Celery client for sending tasks.

    This does NOT load the full worker configuration or register signal handlers.
    It only sets up the broker connection for sending tasks.
    """
    from src.workers.base.worker_config import BaseWorkerConfig

    config = BaseWorkerConfig()

    client = Celery(
        "integrations_client",
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
        worker_hijack_root_logger=False,
    )

    logger.info(
        "Integrations Celery client initialized (broker=%s)",
        config.broker_url.replace(config.redis_password, "****")
        if config.redis_password
        else config.broker_url,
    )

    return client


def get_integrations_celery_client() -> Celery:
    """
    Get or create the singleton Celery client for integrations tasks.

    Thread-safe lazy initialization ensures the client is only created once,
    and only when first needed (not at module import time).
    """
    global _integrations_client

    if _integrations_client is None:
        with _client_lock:
            if _integrations_client is None:
                _integrations_client = _create_integrations_client()

    return _integrations_client


def send_integrations_task(
    task_name: str,
    args: list | None = None,
    kwargs: dict | None = None,
    queue: str = "integrations",
    routing_key: str | None = None,
    headers: dict | None = None,
) -> str:
    """
    Send a task to the integrations worker (synchronous version).

    WARNING: This is a blocking call. In async contexts, use send_integrations_task_async().
    """
    client = get_integrations_celery_client()

    result = client.send_task(
        task_name,
        args=args or [],
        kwargs=kwargs or {},
        queue=queue,
        routing_key=routing_key or queue,
        headers=correlation_headers(headers),
    )

    return str(result.id)


async def send_integrations_task_async(
    task_name: str,
    args: list | None = None,
    kwargs: dict | None = None,
    queue: str = "integrations",
    routing_key: str | None = None,
    headers: dict | None = None,
) -> str:
    """
    Send a task to the integrations worker (async version).

    This runs the blocking send_task() call in a thread pool to avoid
    blocking the asyncio event loop. Use this in FastAPI async endpoints.
    """
    loop = asyncio.get_event_loop()
    ctx = contextvars.copy_context()

    task_id = await loop.run_in_executor(
        _executor,
        ctx.run,
        send_integrations_task,
        task_name,
        args,
        kwargs,
        queue,
        routing_key,
        headers,
    )

    return task_id
