"""Run async coroutines safely from synchronous worker code.

Celery tasks in this codebase are mostly synchronous functions that call async
database and provider code. This module owns the per-thread event loop used by
those tasks and the fork cleanup needed for prefork worker children.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

from src.infra.utils.logger import get_logger

T = TypeVar("T")

logger = get_logger(__name__)
_thread_local = threading.local()


def _clear_async_db_provider_cache(
    owner_loop: asyncio.AbstractEventLoop | None = None,
) -> None:
    """Replace async pools when their owning event loop changes.

    Disposal runs on the owning loop while it is usable. If an external caller
    already closed that loop, a short-lived recovery loop lets asyncpg abort
    connections that can no longer close gracefully.
    """
    recovery_loop: asyncio.AbstractEventLoop | None = None
    try:
        from src.infra.database.factory import close_async_database_provider

        if owner_loop is None or owner_loop.is_closed():
            recovery_loop = asyncio.new_event_loop()
            owner_loop = recovery_loop
        owner_loop.run_until_complete(close_async_database_provider())
    except Exception as e:
        logger.debug("Could not clear async DB provider cache: %s", e)
    finally:
        if recovery_loop is not None:
            recovery_loop.close()


def _reset_async_db_provider_after_fork() -> None:
    """Detach inherited async pools without closing the parent's sockets."""

    try:
        from src.infra.database.factory import (
            reset_async_database_provider_after_fork,
        )

        reset_async_database_provider_after_fork()
    except Exception as e:
        # Fork cleanup is best-effort because process initialization must still
        # replace the inherited event loop even if no DB provider was created.
        logger.debug("Could not reset async DB provider after fork: %s", e)


def _clear_sync_db_provider_cache() -> None:
    """Replace cached sync database engines after fork.

    In prefork workers, child processes inherit the parent's memory including
    any lazily-created sync DB provider singletons. These hold connections/pools
    that are not safe to use across fork boundaries. The provider replaces its
    pools without closing the parent process's underlying connections.
    """
    try:
        from src.infra.database.factory import reset_sync_database_provider_after_fork

        reset_sync_database_provider_after_fork()
    except Exception as e:
        logger.debug("Could not clear sync DB provider cache: %s", e)


def reset_after_fork() -> None:
    """Reset async runtime state inherited by a Celery prefork child.

    Forked children cannot safely reuse the parent's event loop, executor, or
    DB engines. Discarding them here makes the first task in the child create
    fresh asyncpg connections on a child-owned loop.
    """
    _reset_async_db_provider_after_fork()
    _clear_sync_db_provider_cache()

    thread_loop: asyncio.AbstractEventLoop | None = getattr(_thread_local, "loop", None)
    policy_loop: asyncio.AbstractEventLoop | None = None
    with contextlib.suppress(RuntimeError):
        policy_loop = asyncio.get_event_loop()

    for loop in {thread_loop, policy_loop}:
        if loop is None:
            continue
        with contextlib.suppress(Exception):
            if not loop.is_closed():
                loop.close()

    if thread_loop is not None:
        delattr(_thread_local, "loop")

    # Some libraries call asyncio.get_event_loop() directly instead of going
    # through run_async(); detach the inherited policy loop for those paths too.
    with contextlib.suppress(Exception):
        asyncio.set_event_loop(None)


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
            _clear_async_db_provider_cache(loop)

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
    except RuntimeError as e:
        # Common failure mode in Celery workers on Windows: asyncpg connections
        # (or SQLAlchemy async engines/pools) were created on a different event loop
        # and later reused. Clear cached engines so the next retry builds fresh
        # pools on the current loop.
        msg = str(e)
        if "attached to a different loop" in msg or "different loop" in msg:
            logger.warning(
                "Detected event-loop mismatch; clearing async DB caches and re-raising: %s",
                msg,
            )
            _clear_async_db_provider_cache(loop)
        raise
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


__all__ = [
    "run_async",
    "reset_after_fork",
    "_clear_async_db_provider_cache",
    "_clear_sync_db_provider_cache",
    "_reset_async_db_provider_after_fork",
]
