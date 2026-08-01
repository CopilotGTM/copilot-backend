"""Regression tests for sync-to-async worker loop management."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from src.infra.utils import async_runner


def test_shared_loop_runs_thread_pool_coroutines_on_one_event_loop():
    """Threaded Celery tasks must share one loop so async pools stay loop-safe."""
    started = 0
    started_lock = threading.Lock()
    both_started = threading.Event()

    async def overlap() -> int:
        """Wait until both task threads have entered their coroutine."""
        nonlocal started
        with started_lock:
            started += 1
            if started == 2:
                both_started.set()

        while not both_started.is_set():
            await asyncio.sleep(0.001)
        return id(asyncio.get_running_loop())

    async_runner.start_shared_loop()
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            loop_ids = list(
                executor.map(lambda _index: async_runner.run_async(overlap()), range(2))
            )
    finally:
        async_runner.stop_shared_loop()

    assert len(set(loop_ids)) == 1


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


def test_reset_after_fork_discards_inherited_shared_loop(mocker):
    """A prefork child must not submit work to the parent's vanished loop thread."""
    inherited_loop = mocker.Mock()
    inherited_thread = mocker.Mock()
    async_runner._shared_loop = inherited_loop
    async_runner._shared_loop_thread = inherited_thread

    async_runner.reset_after_fork()

    assert async_runner._shared_loop is None
    assert async_runner._shared_loop_thread is None


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
    async_clear = mocker.patch("src.infra.utils.async_runner._reset_async_db_provider_after_fork")
    sync_clear = mocker.patch("src.infra.utils.async_runner._clear_sync_db_provider_cache")

    async_runner.reset_after_fork()

    async_clear.assert_called_once_with()
    sync_clear.assert_called_once_with()


def test_async_cache_clear_awaits_disposal_on_the_current_loop(monkeypatch):
    """An open owning loop must gracefully close its asyncpg connections."""

    calls: list[asyncio.AbstractEventLoop] = []

    async def close_provider() -> None:
        """Record the loop used for active provider disposal."""

        calls.append(asyncio.get_running_loop())

    monkeypatch.setattr(
        "src.infra.database.factory.close_async_database_provider",
        close_provider,
        raising=False,
    )
    loop = asyncio.new_event_loop()
    try:
        async_runner._clear_async_db_provider_cache(loop)
    finally:
        loop.close()

    assert calls == [loop]


def test_async_cache_clear_uses_recovery_loop_when_owner_is_closed(monkeypatch):
    """A closed owner loop still needs deterministic active pool disposal."""

    calls: list[asyncio.AbstractEventLoop] = []

    async def close_provider() -> None:
        """Record the temporary loop used after the owner was closed."""

        calls.append(asyncio.get_running_loop())

    monkeypatch.setattr(
        "src.infra.database.factory.close_async_database_provider",
        close_provider,
        raising=False,
    )
    closed_loop = asyncio.new_event_loop()
    closed_loop.close()

    async_runner._clear_async_db_provider_cache(closed_loop)

    assert len(calls) == 1
    assert calls[0] is not closed_loop
    assert calls[0].is_closed()


def test_async_fork_reset_delegates_to_passive_factory_cleanup(mocker):
    """Fork cleanup must preserve the parent's underlying connections."""

    reset = mocker.patch(
        "src.infra.database.factory.reset_async_database_provider_after_fork",
    )

    async_runner._reset_async_db_provider_after_fork()

    reset.assert_called_once_with()


def test_sync_cache_clear_delegates_to_database_factory(mocker):
    """Fork cleanup must delegate sync-pool ownership to the factory."""

    reset = mocker.patch(
        "src.infra.database.factory.reset_sync_database_provider_after_fork",
    )

    async_runner._clear_sync_db_provider_cache()

    reset.assert_called_once_with()
