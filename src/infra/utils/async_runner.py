from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

from src.infra.utils.logger import get_logger

T = TypeVar("T")

logger = get_logger(__name__)
_thread_local = threading.local()


def _clear_async_db_provider_cache() -> None:
    """Clear cached async database engines when event loop changes.

    This prevents "attached to a different loop" errors when asyncpg
    connections cached in the provider were created on a previous event loop.
    Called when we detect the event loop was recreated.
    """
    try:
        from src.infra.database.factory import _async_instance
        from src.infra.database.provider_async import AsyncSqlAlchemyProvider

        # Cast to concrete implementation to access internal caches
        if _async_instance is not None and isinstance(_async_instance, AsyncSqlAlchemyProvider):
            provider = _async_instance
            # Clear the engine caches without async disposal (old loop may be dead)
            with provider._lock:
                if provider._tenant_engines:
                    logger.info(
                        "Clearing %d stale async tenant engines after loop recreation",
                        len(provider._tenant_engines),
                    )
                    provider._tenant_engines.clear()
                    provider._tenant_pool_order.clear()
                    provider._tenant_locks.clear()
                if provider._master_engine is not None:
                    logger.info("Clearing stale async master engine after loop recreation")
                    provider._master_engine = None
    except Exception as e:
        logger.debug("Could not clear async DB provider cache: %s", e)


def _get_or_create_loop() -> asyncio.AbstractEventLoop:
    """Get or create a persistent event loop for the current thread.

    In Celery solo pool mode, all tasks run in the same thread, so we
    need a persistent loop that doesn't get closed between tasks.

    If a new loop is created (because previous was closed/corrupted),
    we also clear cached async database engines to prevent connection errors.
    """
    loop: asyncio.AbstractEventLoop | None = getattr(_thread_local, "loop", None)
    if loop is None or loop.is_closed():
        # Clear stale DB connections before creating new loop
        if loop is not None and loop.is_closed():
            logger.warning("Event loop was closed, creating new loop and clearing DB caches")
            _clear_async_db_provider_cache()

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _thread_local.loop = loop
    return loop


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    """Run an async coroutine on a persistent thread-local event loop.

    This approach keeps the event loop alive between Celery tasks to prevent
    "Event loop is closed" errors when SQLAlchemy tries to clean up connections
    from previous tasks.

    The loop persists for the lifetime of the worker thread (solo pool mode).
    """
    loop = _get_or_create_loop()

    if loop.is_running():
        raise RuntimeError("run_async() cannot be called while the event loop is running")

    try:
        return loop.run_until_complete(coro)
    finally:
        # Clean up pending tasks after each task execution
        # but DON'T close the loop - keep it for the next task
        try:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                if not task.done():
                    task.cancel()

            # Allow cancellations to complete
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            # Ignore cleanup errors - loop is still usable
            # This is expected during task cleanup in long-running workers
            logger.debug("Task cleanup encountered exception during shutdown", exc_info=True)


__all__ = ["run_async"]
