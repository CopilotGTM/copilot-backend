"""Factory helpers returning singleton provider instances.

Usage (sync):
    from src.infra.database.factory import get_sync_provider
    db = get_sync_provider()
    with db.tenant_session(org_id) as s:
        ...

Usage (async):
    from src.infra.database.factory import get_async_provider
    async with get_async_provider().tenant_session(org_id) as s:
        ...
"""

from __future__ import annotations

import threading

from src.infra.database.interface import AsyncDatabaseProvider, SyncDatabaseProvider
from src.infra.database.provider_async import AsyncSqlAlchemyProvider
from src.infra.database.provider_sync import SyncSqlAlchemyProvider

_sync_instance: SyncDatabaseProvider | None = None
_async_instance: AsyncDatabaseProvider | None = None
_lock = threading.Lock()


def get_sync_provider() -> SyncDatabaseProvider:
    """Return the process-local synchronous provider singleton."""

    global _sync_instance
    if _sync_instance is None:
        with _lock:
            if _sync_instance is None:
                _sync_instance = SyncSqlAlchemyProvider()
    return _sync_instance


def get_async_provider() -> AsyncDatabaseProvider:
    """Return a singleton async-capable database provider.

    This function is synchronous because provider construction is CPU-bound /
    non-awaitable. The returned provider exposes async session/context APIs.
    """
    global _async_instance
    if _async_instance is None:
        with _lock:
            if _async_instance is None:
                _async_instance = AsyncSqlAlchemyProvider()
    return _async_instance


def reset_sync_database_provider_after_fork() -> None:
    """Detach and reset the sync provider inherited by a forked child."""

    global _sync_instance
    with _lock:
        provider = _sync_instance
        _sync_instance = None
    if provider is not None:
        provider.reset_after_fork()


def reset_async_database_provider_after_fork() -> None:
    """Detach async pools inherited by a forked child without closing the parent."""

    global _async_instance
    with _lock:
        provider = _async_instance
        _async_instance = None
    if provider is not None:
        provider.reset_after_fork()


def close_sync_database_provider() -> None:
    """Close and detach the process-local synchronous provider."""

    global _sync_instance
    with _lock:
        provider = _sync_instance
        _sync_instance = None
    if provider is not None:
        provider.dispose()


async def close_async_database_provider() -> None:
    """Actively close and detach the process-local asynchronous provider."""

    global _async_instance
    with _lock:
        provider = _async_instance
        _async_instance = None
    if provider is not None:
        await provider.dispose()


async def close_database_providers() -> None:
    """Close all runtime pools and detach their singleton providers."""

    global _sync_instance, _async_instance
    with _lock:
        sync_provider = _sync_instance
        async_provider = _async_instance
        _sync_instance = None
        _async_instance = None
    if sync_provider is not None:
        sync_provider.dispose()
    if async_provider is not None:
        await async_provider.dispose()


__all__ = [
    "close_async_database_provider",
    "close_sync_database_provider",
    "close_database_providers",
    "get_async_provider",
    "get_sync_provider",
    "reset_async_database_provider_after_fork",
    "reset_sync_database_provider_after_fork",
]
