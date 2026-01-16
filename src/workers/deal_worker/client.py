"""
Lightweight Celery client for sending tasks to the deal worker.

This module provides a minimal Celery client that can be used by the FastAPI
application to enqueue tasks without loading the full worker configuration
and signal handlers. This avoids the overhead of initializing the full Celery
app at import time, which can cause blocking issues during FastAPI startup.

IMPORTANT: The send_task() call in Celery is synchronous and can block when
connecting to Redis (especially with SSL). This module provides async wrappers
that run send_task in a thread pool to avoid blocking the asyncio event loop.

Usage:
    from src.workers.deal_worker.client import send_deal_task_async

    task_id = await send_deal_task_async(
        "deal.match_calendar_to_deals_org",
        kwargs={"organisation_id": org_id},
        queue="deal"
    )
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

from celery import Celery
from sqlalchemy.ext.asyncio import AsyncSession

from src.infra.observability.context import correlation_headers
from src.infra.utils.logger import get_logger

logger = get_logger("deal_worker_client")

# Thread-safe lazy initialization
_client_lock = threading.Lock()
_deal_client: Celery | None = None

# Thread pool for running blocking Celery operations
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="celery_deal_client")


def _create_deal_client() -> Celery:
    """
    Create a minimal Celery client for sending tasks.

    This does NOT load the full worker configuration or register signal handlers.
    It only sets up the broker connection for sending tasks.
    """
    from src.workers.base.worker_config import BaseWorkerConfig

    config = BaseWorkerConfig()

    client = Celery(
        "deal_client",
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
        "Deal Celery client initialized (broker=%s)",
        config.broker_url.replace(config.redis_password, "****")
        if config.redis_password
        else config.broker_url,
    )

    return client


def get_deal_celery_client() -> Celery:
    """
    Get or create the singleton Celery client for deal tasks.

    Thread-safe lazy initialization ensures the client is only created once,
    and only when first needed (not at module import time).
    """
    global _deal_client

    if _deal_client is None:
        with _client_lock:
            if _deal_client is None:
                _deal_client = _create_deal_client()

    return _deal_client


def send_deal_task(
    task_name: str,
    args: list | None = None,
    kwargs: dict | None = None,
    queue: str = "deal",
    routing_key: str | None = None,
    headers: dict | None = None,
) -> str:
    """
    Send a task to the deal worker (synchronous version).

    WARNING: This is a blocking call. In async contexts, use send_deal_task_async().
    """
    client = get_deal_celery_client()

    result = client.send_task(
        task_name,
        args=args or [],
        kwargs=kwargs or {},
        queue=queue,
        routing_key=routing_key or queue,
        headers=correlation_headers(headers),
    )

    return str(result.id)


async def send_deal_task_async(
    task_name: str,
    args: list | None = None,
    kwargs: dict | None = None,
    queue: str = "deal",
    routing_key: str | None = None,
    headers: dict | None = None,
) -> str:
    """
    Send a task to the deal worker (async version).

    This runs the blocking send_task() call in a thread pool to avoid
    blocking the asyncio event loop. Use this in FastAPI async endpoints.
    """
    loop = asyncio.get_event_loop()
    ctx = contextvars.copy_context()

    task_id = await loop.run_in_executor(
        _executor,
        ctx.run,
        send_deal_task,
        task_name,
        args,
        kwargs,
        queue,
        routing_key,
        headers,
    )

    return task_id


class DealWorkerAsyncClient:
    """Async client for queueing deal worker jobs with job record creation.

    This client creates job records in the database before queueing Celery tasks,
    providing a job_id that can be used to track job status via the API.
    """

    def __init__(
        self,
        db_session: AsyncSession,
        organisation_id: UUID,
    ):
        """Initialize the async client.

        Args:
            db_session: Async database session for job record creation
            organisation_id: UUID of the organisation
        """
        self.db_session = db_session
        self.organisation_id = organisation_id
