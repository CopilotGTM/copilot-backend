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
_shared_loop: asyncio.AbstractEventLoop | None = None
_shared_loop_thread: threading.Thread | None = None
_shared_loop_lock = threading.Lock()


def _serve_shared_loop(
    loop: asyncio.AbstractEventLoop,
    started: threading.Event,
) -> None:
    """Run the worker-owned event loop until Celery begins graceful shutdown."""
    asyncio.set_event_loop(loop)
    started.set()
    try:
        loop.run_forever()
    finally:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()


def start_shared_loop() -> None:
    """Start one asyncio loop shared by synchronous tasks in a threaded worker.

    Async database engines are bound to their creating loop. Routing every
    thread through this loop lets Celery overlap I/O without allowing separate
    task threads to create incompatible engine pools. Coroutines must await
    their child tasks because per-call cleanup would cancel other chat jobs.
    """
    global _shared_loop, _shared_loop_thread

    with _shared_loop_lock:
        if _shared_loop_thread is not None and _shared_loop_thread.is_alive():
            return

        loop = asyncio.new_event_loop()
        started = threading.Event()
        thread = threading.Thread(
            target=_serve_shared_loop,
            args=(loop, started),
            name="worker-asyncio-loop",
            daemon=True,
        )
        _shared_loop = loop
        _shared_loop_thread = thread
        thread.start()

    if not started.wait(timeout=5):
        stop_shared_loop()
        raise RuntimeError("Shared asyncio loop did not start")


def stop_shared_loop() -> None:
    """Stop and release the threaded worker's shared asyncio loop."""
    global _shared_loop, _shared_loop_thread

    with _shared_loop_lock:
        loop = _shared_loop
        thread = _shared_loop_thread
        _shared_loop = None
        _shared_loop_thread = None

    if loop is None or thread is None:
        return

    if not loop.is_closed():
        loop.call_soon_threadsafe(loop.stop)
    if thread is not threading.current_thread():
        thread.join(timeout=5)


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
    global _shared_loop, _shared_loop_thread, _shared_loop_lock

    # Fork copies the parent's "running" loop state but not its owner thread.
    # Drop those references without touching parent-owned resources.
    _shared_loop = None
    _shared_loop_thread = None
    _shared_loop_lock = threading.Lock()

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
    """Run an async coroutine on the configured worker event loop.

    Threaded workers submit to one explicitly started shared loop so async
    connection pools keep one owner. Other callers retain the persistent
    thread-local loop used by solo and prefork workers.
    """
    shared_loop = _shared_loop
    if shared_loop is not None and shared_loop.is_running():
        if threading.current_thread() is _shared_loop_thread:
            coro.close()
            raise RuntimeError("run_async() cannot block the shared event-loop thread")
        return asyncio.run_coroutine_threadsafe(coro, shared_loop).result()

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
    "start_shared_loop",
    "stop_shared_loop",
    "reset_after_fork",
    "_clear_async_db_provider_cache",
    "_clear_sync_db_provider_cache",
    "_reset_async_db_provider_after_fork",
]
