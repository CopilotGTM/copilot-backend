"""
Lightweight Celery client for sending tasks to the organization worker.

This module provides a minimal Celery client that can be used by the FastAPI
application to enqueue tasks without loading the full worker configuration
and signal handlers. This avoids the overhead of initializing the full Celery
app at import time, which can cause blocking issues during FastAPI startup.

IMPORTANT: The send_task() call in Celery is synchronous and can block when
connecting to Redis (especially with SSL). This module provides async wrappers
that run send_task in a thread pool to avoid blocking the asyncio event loop.

Usage (New Async Pattern - Preferred):
    from src.workers.organization_worker.client import send_organization_create_task

    task_id = await send_organization_create_task(
        job_id="job-uuid",
        admin_id="admin-uuid",
        organisation_name="Acme Corp",
        org_details={"country": "US"},
    )
    # Returns immediately, client uses SSE to track progress

Usage (Legacy - Deprecated):
    from src.workers.organization_worker.client import send_organization_task_and_wait_async

    result = await send_organization_task_and_wait_async(
        "organization.create",
        kwargs={"admin_id": admin_id, "organisation_name": name},
        timeout=120,  # Blocks for up to 2 minutes!
    )
"""

import asyncio
import contextvars
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor

from celery import Celery

from src.infra.observability.context import correlation_headers
from src.infra.utils.logger import get_logger

logger = get_logger("organization_worker_client")

# Thread-safe lazy initialization
_client_lock = threading.Lock()
_organization_client: Celery | None = None

# Thread pool for running blocking Celery operations
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="celery_org_client")


def _create_organization_client() -> Celery:
    """
    Create a minimal Celery client for sending tasks.

    This does NOT load the full worker configuration or register signal handlers.
    It only sets up the broker connection for sending tasks.
    """
    from src.workers.base.worker_config import BaseWorkerConfig

    config = BaseWorkerConfig()

    client = Celery(
        "organization_client",
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
        "Organization Celery client initialized (broker=%s)",
        config.broker_url.replace(config.redis_password, "****")
        if config.redis_password
        else config.broker_url,
    )

    return client


def get_organization_celery_client() -> Celery:
    """
    Get or create the singleton Celery client for organization tasks.

    Thread-safe lazy initialization ensures the client is only created once,
    and only when first needed (not at module import time).
    """
    global _organization_client

    if _organization_client is None:
        with _client_lock:
            if _organization_client is None:
                _organization_client = _create_organization_client()

    return _organization_client


def send_organization_task(
    task_name: str,
    args: list | None = None,
    kwargs: dict | None = None,
    queue: str = "organization",
    routing_key: str | None = None,
    headers: dict | None = None,
) -> str:
    """
    Send a task to the organization worker (synchronous version).

    WARNING: This is a blocking call. In async contexts, use send_organization_task_async().
    """
    client = get_organization_celery_client()

    result = client.send_task(
        task_name,
        args=args or [],
        kwargs=kwargs or {},
        queue=queue,
        routing_key=routing_key or queue,
        headers=correlation_headers(headers),
    )

    return str(result.id)


def send_organization_task_and_wait(
    task_name: str,
    args: list | None = None,
    kwargs: dict | None = None,
    queue: str = "organization",
    routing_key: str | None = None,
    timeout: int = 120,
    headers: dict | None = None,
):
    """
    Send a task and wait for the result (synchronous version).

    WARNING: This is a blocking call. In async contexts, use send_organization_task_and_wait_async().

    DEPRECATED: Use send_organization_create_task() with SSE streaming instead.
    This function blocks the event loop for up to `timeout` seconds.
    """
    warnings.warn(
        "send_organization_task_and_wait is deprecated. "
        "Use send_organization_create_task() with SSE streaming instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    client = get_organization_celery_client()

    result = client.send_task(
        task_name,
        args=args or [],
        kwargs=kwargs or {},
        queue=queue,
        routing_key=routing_key or queue,
        headers=correlation_headers(headers),
    )

    return result.get(timeout=timeout)


async def send_organization_task_async(
    task_name: str,
    args: list | None = None,
    kwargs: dict | None = None,
    queue: str = "organization",
    routing_key: str | None = None,
    headers: dict | None = None,
) -> str:
    """
    Send a task to the organization worker (async version).

    This runs the blocking send_task() call in a thread pool to avoid
    blocking the asyncio event loop. Use this in FastAPI async endpoints.
    """
    loop = asyncio.get_event_loop()
    ctx = contextvars.copy_context()

    task_id = await loop.run_in_executor(
        _executor,
        ctx.run,
        send_organization_task,
        task_name,
        args,
        kwargs,
        queue,
        routing_key,
        headers,
    )

    return task_id


async def send_organization_task_and_wait_async(
    task_name: str,
    args: list | None = None,
    kwargs: dict | None = None,
    queue: str = "organization",
    routing_key: str | None = None,
    timeout: int = 120,
    headers: dict | None = None,
):
    """
    Send a task and wait for the result (async version).

    This runs the blocking send_task().get() call in a thread pool to avoid
    blocking the asyncio event loop. Use this in FastAPI async endpoints
    when you need to wait for the task result.

    DEPRECATED: Use send_organization_create_task() with SSE streaming instead.
    This function blocks for up to `timeout` seconds waiting for result.
    """
    warnings.warn(
        "send_organization_task_and_wait_async is deprecated. "
        "Use send_organization_create_task() with SSE streaming instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    loop = asyncio.get_event_loop()
    ctx = contextvars.copy_context()

    # Create a wrapper function that includes the timeout
    def _send_and_wait():
        return send_organization_task_and_wait(
            task_name,
            args,
            kwargs,
            queue,
            routing_key,
            timeout=timeout,
            headers=headers,
        )

    result = await loop.run_in_executor(_executor, ctx.run, _send_and_wait)

    return result


# =============================================================================
# NEW ASYNC PATTERN - Fire-and-forget with SSE streaming
# =============================================================================


async def send_organization_create_task(
    job_id: str,
    admin_id: str,
    organisation_name: str,
    org_details: dict,
    queue: str = "organization",
    headers: dict | None = None,
) -> str:
    """
    Send organization create task (fire-and-forget).

    Returns immediately after enqueuing task. Client should use SSE endpoint
    to stream job status updates.

    Args:
        job_id: UUID of the OrganisationJob record (created before calling)
        admin_id: UUID of the admin creating the organisation
        organisation_name: Name for the new organisation
        org_details: Additional organisation configuration
        queue: Celery queue name (default: organization)
        headers: Optional headers to pass to the task

    Returns:
        Celery task ID (not the job_id)
    """
    return await send_organization_task_async(
        task_name="organization.create",
        kwargs={
            "job_id": job_id,
            "admin_id": admin_id,
            "organisation_name": organisation_name,
            "org_details": org_details,
        },
        queue=queue,
        headers=headers,
    )
