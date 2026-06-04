"""Regression tests for sync-to-async worker loop management."""

from __future__ import annotations

import asyncio

import pytest
from src.infra.utils import async_runner


def test_reset_after_fork_discards_inherited_thread_local_loop():
    """Prefork children must not reuse the parent event loop or executor."""
    parent_loop = asyncio.new_event_loop()
    async_runner._thread_local.loop = parent_loop

    async_runner.reset_after_fork()

    async def current_loop_id() -> int:
        """Return the loop created after the fork reset."""
        return id(asyncio.get_running_loop())

    try:
        child_loop_id = async_runner.run_async(current_loop_id())
    finally:
        loop = getattr(async_runner._thread_local, "loop", None)
        if loop is not None and not loop.is_closed():
            loop.close()
        if not parent_loop.is_closed():
            parent_loop.close()
        if hasattr(async_runner._thread_local, "loop"):
            del async_runner._thread_local.loop

    assert child_loop_id != id(parent_loop)


def test_reset_after_fork_clears_asyncio_policy_loop():
    """Fork reset must also detach the loop visible through asyncio policy APIs."""
    parent_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(parent_loop)

    async_runner.reset_after_fork()

    try:
        with pytest.raises(RuntimeError, match="There is no current event loop"):
            asyncio.get_event_loop()
    finally:
        loop = getattr(async_runner._thread_local, "loop", None)
        if loop is not None and not loop.is_closed():
            loop.close()
        if not parent_loop.is_closed():
            parent_loop.close()
        asyncio.set_event_loop(None)


def test_reset_after_fork_closes_policy_loop_without_thread_local_loop():
    """Policy-only inherited loops should be closed, not only detached."""
    parent_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(parent_loop)
    if hasattr(async_runner._thread_local, "loop"):
        del async_runner._thread_local.loop

    async_runner.reset_after_fork()

    try:
        assert parent_loop.is_closed()
    finally:
        if not parent_loop.is_closed():
            parent_loop.close()
        asyncio.set_event_loop(None)


def test_reset_after_fork_clears_database_provider_caches(mocker):
    """Fork reset must keep DB engine cleanup coupled to loop cleanup."""
    async_clear = mocker.patch("src.infra.utils.async_runner._clear_async_db_provider_cache")
    sync_clear = mocker.patch("src.infra.utils.async_runner._clear_sync_db_provider_cache")

    async_runner.reset_after_fork()

    async_clear.assert_called_once_with()
    sync_clear.assert_called_once_with()
