from __future__ import annotations

import asyncio
import threading
from collections.abc import Coroutine
from contextlib import suppress
from typing import Any, TypeVar

T = TypeVar("T")

_thread_local = threading.local()


def _ensure_thread_loop() -> asyncio.AbstractEventLoop:
    """Return a persistent event loop for the current thread."""
    loop: asyncio.AbstractEventLoop | None = getattr(_thread_local, "loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _thread_local.loop = loop
    return loop


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    """Run an async coroutine on the thread-local event loop without closing it."""
    loop = _ensure_thread_loop()

    if loop.is_running():
        raise RuntimeError("run_async() cannot be called while the managed loop is running")

    task = asyncio.ensure_future(coro, loop=loop)
    try:
        return loop.run_until_complete(task)
    except Exception:
        if not task.done():
            task.cancel()
            with suppress(Exception):
                loop.run_until_complete(task)
        raise


def get_loop() -> asyncio.AbstractEventLoop:
    """Expose the managed event loop (mainly for testing/inspection)."""
    return _ensure_thread_loop()


__all__ = ["run_async", "get_loop"]
