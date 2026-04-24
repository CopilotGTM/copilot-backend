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
from typing import Any

from celery import Celery
from celery.canvas import Signature

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

    Does NOT register signal handlers or auto-discover tasks. Does apply the
    integrations worker's task_routes + default queue so:

      - signature-based dispatch (e.g. task_signature + chain.apply_async)
        routes each task to the queue declared in task_routes rather than
        falling through to Celery's global default "celery" queue, which
        the integrations worker does not listen on.
      - callers that omit an explicit `queue=` argument to send_task still
        get routed correctly.
    """
    from src.workers.integrations_worker.config import IntegrationsWorkerConfig

    config = IntegrationsWorkerConfig()

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
        broker_transport_options={
            "socket_timeout": 10,
            "socket_connect_timeout": 10,
            "socket_keepalive": True,
            "retry_on_timeout": True,
        },
        worker_hijack_root_logger=False,
        # Routing — must match the worker's view so signature-based dispatch
        # lands on a queue the worker actually consumes.
        task_default_queue=config.task_default_queue,
        task_default_exchange=config.task_default_exchange,
        task_default_exchange_type=config.task_default_exchange_type,
        task_default_routing_key=config.task_default_routing_key,
        task_queues=config.task_queues,
        task_routes=config.task_routes,
    )

    masked_broker = (
        config.broker_url.replace(config.redis_password, "****")
        if config.redis_password
        else config.broker_url
    )
    logger.debug("Integrations Celery client initialized", extra={"broker": masked_broker})

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


def task_signature(
    task_name: str,
    *,
    kwargs: dict[str, Any] | None = None,
) -> Signature:
    """Build an immutable Celery signature against the integrations client.

    Used by chain builders so that:
      - Routing follows `task_routes` configured on the integrations client.
        This function does NOT override queue or routing_key — per-task queue
        routing is a single source of truth, not something callers reinvent.
      - Signatures are immutable (`.si(...)` shape) so chained tasks don't
        receive the previous task's return value as a positional arg. All
        our chain tasks take `organisation_id` and `integration_id` by kwarg.
    """
    client = get_integrations_celery_client()
    return client.signature(
        task_name,
        kwargs=kwargs or {},
        immutable=True,
        headers=correlation_headers(None),
    )


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
