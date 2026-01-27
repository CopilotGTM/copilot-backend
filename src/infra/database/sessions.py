"""Session acquisition functions for workers and services.

This module provides database session acquisition functions that have NO FastAPI
dependencies. Use these in workers, services, and any non-API code.

For FastAPI endpoints, use the dependencies in src/api/dependencies/ which wrap
these functions with authentication and dependency injection.

Functions:
    - acquire_master_session(): Sync session for master (shared) database
    - acquire_master_async_session(): Async session for master database
    - acquire_org_session(org_id): Sync session for tenant database
    - acquire_org_async_session(org_id): Async session for tenant database
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Generator, Iterator
from contextlib import asynccontextmanager, contextmanager
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from src.infra.database.factory import get_async_provider, get_sync_provider

# ============================================================================
# MASTER SESSION FUNCTIONS
# ============================================================================


def _sync_master_session_gen() -> Generator[Session, None, None]:
    """Generator function that yields a synchronous master database session."""
    provider = get_sync_provider()
    with provider.master_session() as session:
        yield session


def acquire_master_session() -> Generator[Session, None, None]:
    """Acquire a synchronous master database session.

    Use this for sync code outside FastAPI dependency system (e.g., Celery tasks).

    Example:
        for session in acquire_master_session():
            result = session.execute(select(User))
            users = result.scalars().all()
    """
    return _sync_master_session_gen()


@contextmanager
def master_session_scope() -> Iterator[Session]:
    """Context manager for a synchronous master database session.

    Preferred for non-FastAPI code to avoid generator misuse and ensure
    sessions are always released.
    """
    provider = get_sync_provider()
    with provider.master_session() as session:
        yield session


async def _async_master_session_gen() -> AsyncGenerator[AsyncSession, None]:
    """Generator function that yields an asynchronous master database session."""
    provider = get_async_provider()
    async with provider.master_session() as session:
        yield session


def acquire_master_async_session() -> AsyncGenerator[AsyncSession, None]:
    """Acquire an asynchronous master database session.

    Use this for async code outside FastAPI dependency system (e.g., async Celery tasks).

    Example:
        async for session in acquire_master_async_session():
            result = await session.execute(select(User))
            users = result.scalars().all()
    """
    return _async_master_session_gen()


@asynccontextmanager
async def master_async_session_scope() -> AsyncGenerator[AsyncSession, None]:
    """Async context manager for a master database session.

    Preferred for non-FastAPI async code to avoid generator misuse and ensure
    sessions are always released.
    """
    provider = get_async_provider()
    async with provider.master_session() as session:
        yield session


# ============================================================================
# ORGANISATION (TENANT) SESSION FUNCTIONS
# ============================================================================


def _sync_org_session_gen(organisation_id: UUID) -> Iterator[Session]:
    """Generator function that yields a synchronous organisation database session."""
    provider = get_sync_provider()
    with provider.tenant_session(str(organisation_id)) as session:  # type: ignore
        yield session


def acquire_org_session(organisation_id: UUID) -> Iterator[Session]:
    """Acquire a synchronous organisation (tenant) database session.

    Use this for sync code outside FastAPI dependency system (e.g., Celery tasks).

    Args:
        organisation_id: UUID of the organisation/tenant

    Example:
        for session in acquire_org_session(org_uuid):
            result = session.execute(select(Deal))
            deals = result.scalars().all()
    """
    return _sync_org_session_gen(organisation_id)


@contextmanager
def org_session_scope(organisation_id: UUID | str) -> Iterator[Session]:
    """Context manager for a synchronous organisation (tenant) session.

    Accepts UUID or string for convenience to match existing call sites.
    """
    provider = get_sync_provider()
    with provider.tenant_session(str(organisation_id)) as session:  # type: ignore
        yield session


async def _async_org_session_gen(organisation_id: UUID) -> AsyncIterator[AsyncSession]:
    """Generator function that yields an asynchronous organisation database session."""
    provider = get_async_provider()
    async with provider.tenant_session(str(organisation_id)) as session:  # type: ignore
        yield session


async def acquire_org_async_session(
    organisation_id: UUID,
) -> AsyncGenerator[AsyncSession, None]:
    """Acquire an asynchronous organisation (tenant) database session.

    Use this for async code outside FastAPI dependency system (e.g., async Celery tasks).

    Args:
        organisation_id: UUID of the organisation/tenant

    Example:
        async for session in acquire_org_async_session(org_uuid):
            result = await session.execute(select(Deal))
            deals = result.scalars().all()
    """
    async for s in _async_org_session_gen(organisation_id):
        yield s


@asynccontextmanager
async def org_async_session_scope(
    organisation_id: UUID | str,
) -> AsyncGenerator[AsyncSession, None]:
    """Async context manager for an organisation (tenant) session.

    Accepts UUID or string for convenience to match existing call sites.
    """
    provider = get_async_provider()
    async with provider.tenant_session(str(organisation_id)) as session:  # type: ignore
        yield session


__all__ = [
    "acquire_master_session",
    "acquire_master_async_session",
    "acquire_org_session",
    "acquire_org_async_session",
    "master_session_scope",
    "master_async_session_scope",
    "org_session_scope",
    "org_async_session_scope",
]
