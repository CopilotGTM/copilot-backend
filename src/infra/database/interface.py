"""Database abstraction layer interfaces (new independent implementation).

We expose provider protocols with a consistent naming scheme:
    - master_session(): context manager / async context manager for privileged DB operations.
    - tenant_session(org_id): context manager bound to a tenant (role + schema) session.

This replaces the earlier org_session / legacy adapters and is intentionally
minimal to keep higher layers decoupled from SQLAlchemy specifics.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager, AbstractContextManager
from typing import Any, Protocol
from uuid import UUID


class SyncDatabaseProvider(Protocol):
    def master_session(self) -> AbstractContextManager[Any]: ...
    def tenant_session(self, org_id: UUID) -> AbstractContextManager[Any]: ...
    def invalidate_tenant(self, org_id: UUID) -> None: ...


class AsyncDatabaseProvider(Protocol):
    def master_session(self) -> AbstractAsyncContextManager[Any]: ...
    def tenant_session(self, org_id: UUID) -> AbstractAsyncContextManager[Any]: ...
    async def invalidate_tenant(self, org_id: UUID) -> None: ...


__all__ = ["SyncDatabaseProvider", "AsyncDatabaseProvider"]
