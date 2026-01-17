"""Master-level database session dependencies (sync + async).

FastAPI-specific dependencies that wrap the core session functions from infra.
Use get_master_session/get_master_async_session in FastAPI endpoints.

For non-FastAPI code (workers, services), import directly from:
    from src.infra.database.sessions import acquire_master_session
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

# Import core functions from infra (no FastAPI dependencies)
from src.infra.database.sessions import (
    acquire_master_async_session as _acquire_master_async_session,
)
from src.infra.database.sessions import (
    acquire_master_session as _acquire_master_session,
)


def get_master_session() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a master-scoped Session.

    Use in endpoint signatures: `session: Session = Depends(get_master_session)`.
    """
    yield from _acquire_master_session()


async def get_master_async_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an async master-scoped Session.

    Use in endpoint signatures: `session: AsyncSession = Depends(get_master_async_session)`.
    """
    async for s in _acquire_master_async_session():
        yield s


# Re-export for backwards compatibility
# NOTE: For new code, import from src.infra.database.sessions instead
def acquire_master_session() -> Generator[Session, None, None]:
    """Acquire a sync master session. For new code, use src.infra.database.sessions."""
    return _acquire_master_session()


def acquire_master_async_session() -> AsyncGenerator[AsyncSession, None]:
    """Acquire an async master session. For new code, use src.infra.database.sessions."""
    return _acquire_master_async_session()


__all__ = [
    "get_master_session",
    "acquire_master_session",
    "get_master_async_session",
    "acquire_master_async_session",
]
