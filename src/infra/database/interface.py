"""Database abstraction layer interfaces (new independent implementation).

We expose provider protocols with a consistent naming scheme:
    - master_session(): context manager / async context manager for privileged DB operations.
    - tenant_session(org_id): context manager bound to a tenant (role + schema) session.

This replaces the earlier org_session / legacy adapters and is intentionally
minimal to keep higher layers decoupled from SQLAlchemy specifics.
"""

from __future__ import annotations
from typing import Protocol, Iterator, AsyncIterator, Any


class SyncDatabaseProvider(Protocol):
    def master_session(self) -> Iterator[Any]: ...
    def tenant_session(self, org_id: str) -> Iterator[Any]: ...
    def invalidate_tenant(self, org_id: str) -> None: ...


class AsyncDatabaseProvider(Protocol):
    async def master_session(self) -> AsyncIterator[Any]: ...
    async def tenant_session(self, org_id: str) -> AsyncIterator[Any]: ...
    async def invalidate_tenant(self, org_id: str) -> None: ...


__all__ = ["SyncDatabaseProvider", "AsyncDatabaseProvider"]
