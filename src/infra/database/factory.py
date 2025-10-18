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


__all__ = ["get_sync_provider", "get_async_provider"]
