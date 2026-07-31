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
