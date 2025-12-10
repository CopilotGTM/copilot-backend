"""
Lightweight Celery client for sending tasks to the chat worker.

This module provides a minimal Celery client that can be used by the FastAPI
application to enqueue tasks without loading the full worker configuration
and signal handlers. This avoids the overhead of initializing the full Celery
app at import time, which can cause blocking issues during FastAPI startup.

IMPORTANT: The send_task() call in Celery is synchronous and can block when
connecting to Redis (especially with SSL). This module provides async wrappers
that run send_task in a thread pool to avoid blocking the asyncio event loop.

Usage:
    from src.workers.chat_worker.client import send_chat_task_async

    task_id = await send_chat_task_async(
        "chat.process_message",
        args=[org_id, job_id],
        queue="chat_messages"
    )
"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

from celery import Celery

from src.infra.utils.logger import get_logger

logger = get_logger("chat_worker_client")

# Thread-safe lazy initialization
_client_lock = threading.Lock()
_chat_client: Celery | None = None

# Thread pool for running blocking Celery operations
# Use a small pool since send_task is typically fast once connected
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="celery_client")


def _create_chat_client() -> Celery:
    """
    Create a minimal Celery client for sending tasks.

    This does NOT load the full worker configuration or register signal handlers.
    It only sets up the broker connection for sending tasks.
    """
    from src.workers.base.worker_config import BaseWorkerConfig

    # Use base config to get broker URL and SSL settings
    config = BaseWorkerConfig()

    # Create a minimal Celery app for sending tasks only
    client = Celery(
        "chat_client",
        broker=config.broker_url,
        backend=config.backend_url,
    )

    # Apply SSL options if present
    if config.ssl_config:
        client.conf.broker_use_ssl = config.ssl_config
        client.conf.redis_backend_use_ssl = config.ssl_config

    # Minimal configuration for task sending
    client.conf.update(
        task_serializer="json",
        accept_content=["json"],
        result_serializer="json",
        timezone="UTC",
        enable_utc=True,
        # Connection settings for reliability
        broker_connection_retry_on_startup=True,
        broker_connection_retry=True,
        broker_connection_max_retries=5,
        # Connection timeout to prevent indefinite blocking
        broker_connection_timeout=10,
        # Don't hijack logging
        worker_hijack_root_logger=False,
    )

    logger.info(
        "Chat Celery client initialized (broker=%s)",
        config.broker_url.replace(config.redis_password, "****")
        if config.redis_password
        else config.broker_url,
    )

    return client


def get_chat_celery_client() -> Celery:
    """
    Get or create the singleton Celery client for chat tasks.

    Thread-safe lazy initialization ensures the client is only created once,
    and only when first needed (not at module import time).

    Returns:
        Celery: A minimal Celery client for sending tasks.
    """
    global _chat_client

    if _chat_client is None:
        with _client_lock:
            # Double-check locking pattern
            if _chat_client is None:
                _chat_client = _create_chat_client()

    return _chat_client


def send_chat_task(
    task_name: str,
    args: list | None = None,
    kwargs: dict | None = None,
    queue: str = "chat_messages",
    routing_key: str | None = None,
) -> str:
    """
    Send a task to the chat worker (synchronous version).

    WARNING: This is a blocking call. In async contexts, use send_chat_task_async().

    Args:
        task_name: The name of the task (e.g., "chat.process_message")
        args: Positional arguments for the task
        kwargs: Keyword arguments for the task
        queue: The queue to send the task to (default: "chat_messages")
        routing_key: The routing key (defaults to queue name)

    Returns:
        str: The task ID

    Raises:
        Exception: If the task cannot be sent
    """
    client = get_chat_celery_client()

    result = client.send_task(
        task_name,
        args=args or [],
        kwargs=kwargs or {},
        queue=queue,
        routing_key=routing_key or queue,
    )

    return str(result.id)


async def send_chat_task_async(
    task_name: str,
    args: list | None = None,
    kwargs: dict | None = None,
    queue: str = "chat_messages",
    routing_key: str | None = None,
) -> str:
    """
    Send a task to the chat worker (async version).

    This runs the blocking send_task() call in a thread pool to avoid
    blocking the asyncio event loop. Use this in FastAPI async endpoints.

    Args:
        task_name: The name of the task (e.g., "chat.process_message")
        args: Positional arguments for the task
        kwargs: Keyword arguments for the task
        queue: The queue to send the task to (default: "chat_messages")
        routing_key: The routing key (defaults to queue name)

    Returns:
        str: The task ID

    Raises:
        Exception: If the task cannot be sent
    """
    loop = asyncio.get_event_loop()

    # Run the blocking send_task in the thread pool
    task_id = await loop.run_in_executor(
        _executor,
        send_chat_task,
        task_name,
        args,
        kwargs,
        queue,
        routing_key,
    )

    return task_id
