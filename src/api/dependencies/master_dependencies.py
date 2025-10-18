"""Master-level database session dependencies (sync + async).

Centralizes acquisition of master (shared/public) database sessions for both
sync and async contexts, backed by provider singletons. Use the sync or async
variant that matches your endpoint/service.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from src.infra.database.factory import get_async_provider, get_sync_provider


def _sync_master_session_gen() -> Iterator[Session]:
    """Generator function that yields a synchronous master database session."""
    # Retrieve the synchronous database provider
    provider = get_sync_provider()
    # Open a new session and yield it to the caller
    with provider.master_session() as session:  # type: ignore
        yield session


def get_master_session() -> Iterator[Session]:
    """FastAPI dependency yielding a master-scoped Session.

    Use in endpoint signatures: `session: Session = Depends(get_master_session)`.
    """
    # Delegate to the generator function to yield the session
    yield from _sync_master_session_gen()


def acquire_master_session() -> Iterator[Session]:
    """Imperative helper for sync code outside FastAPI dependency system."""
    # Return the generator for manual iteration
    return _sync_master_session_gen()


async def _async_master_session_gen() -> AsyncIterator[AsyncSession]:
    """Generator function that yields an asynchronous master database session."""
    # Retrieve the asynchronous database provider
    provider = get_async_provider()
    # Open a new async session and yield it to the caller
    async with provider.master_session() as session:  # type: ignore
        yield session


async def get_master_async_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding an async master-scoped Session.

    Use in endpoint signatures: `session: AsyncSession = Depends(get_master_async_session)`.
    """
    # Asynchronously iterate over the generator and yield the session
    async for s in _async_master_session_gen():
        yield s


def acquire_master_async_session() -> AsyncIterator[AsyncSession]:
    """Imperative helper for async code outside FastAPI dependency system."""
    # Return the async generator for manual iteration
    return _async_master_session_gen()


# A list of all public members of this module
__all__ = [
    "get_master_session",
    "acquire_master_session",
    "get_master_async_session",
    "acquire_master_async_session",
]
