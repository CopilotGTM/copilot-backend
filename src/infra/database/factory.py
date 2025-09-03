"""Factory helpers returning singleton provider instances.

Usage (sync):
    from src.infra.database.factory import get_sync_provider
    db = get_sync_provider()
    with db.tenant_session(org_id) as s:
        ...

Usage (async):
    from src.infra.database.factory import get_async_provider
    async with (await get_async_provider()).tenant_session(org_id) as s:
        ...
"""
from __future__ import annotations

import threading
from typing import Optional

from src.infra.database.provider_sync import SyncSqlAlchemyProvider
from src.infra.database.provider_async import AsyncSqlAlchemyProvider
from src.infra.database.interface import SyncDatabaseProvider, AsyncDatabaseProvider

_sync_instance: Optional[SyncDatabaseProvider] = None
_async_instance: Optional[AsyncDatabaseProvider] = None
_lock = threading.Lock()


def get_sync_provider() -> SyncDatabaseProvider:
    global _sync_instance
    if _sync_instance is None:
        with _lock:
            if _sync_instance is None:
                _sync_instance = SyncSqlAlchemyProvider()
    return _sync_instance

async def get_async_provider() -> AsyncDatabaseProvider:
    global _async_instance
    if _async_instance is None:
        with _lock:
            if _async_instance is None:
                _async_instance = AsyncSqlAlchemyProvider()
    return _async_instance

__all__ = ["get_sync_provider", "get_async_provider"]
