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
    """Contract for synchronous master and shared-tenant database access."""

    def master_session(self) -> AbstractContextManager[Any]:
        """Open an unrestricted control-plane session."""

        ...

    def tenant_session(self, org_id: UUID) -> AbstractContextManager[Any]:
        """Open a session protected by transaction-local tenant identity."""

        ...

    def invalidate_tenant(self, org_id: UUID) -> None:
        """Replace the shared tenant pool after a credential or network change."""

        ...

    def dispose(self) -> None:
        """Close every pool during normal process shutdown."""

        ...

    def reset_after_fork(self) -> None:
        """Replace inherited pools without closing parent-process connections."""

        ...


class AsyncDatabaseProvider(Protocol):
    """Contract for asynchronous master and shared-tenant database access."""

    def master_session(self) -> AbstractAsyncContextManager[Any]:
        """Open an unrestricted control-plane session."""

        ...

    def tenant_session(self, org_id: UUID) -> AbstractAsyncContextManager[Any]:
        """Open a session protected by transaction-local tenant identity."""

        ...

    async def invalidate_tenant(self, org_id: UUID) -> None:
        """Replace the shared tenant pool after a credential or network change."""

        ...

    async def dispose(self) -> None:
        """Close every pool during normal process shutdown."""

        ...

    def reset_after_fork(self) -> None:
        """Replace loop-bound inherited pools without performing async I/O."""

        ...


__all__ = ["SyncDatabaseProvider", "AsyncDatabaseProvider"]
