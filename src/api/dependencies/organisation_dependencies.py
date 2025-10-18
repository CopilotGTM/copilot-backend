"""Organisation-level database session dependencies (sync + async).

Centralizes acquisition of organisation (tenant) database sessions for both
sync and async contexts, backed by provider singletons. Use the sync or async
variant that matches your endpoint/service.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from uuid import UUID

from fastapi import Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from src.domain.models.authentication import Authentication
from src.domain.models.master_tables import User
from src.infra.auth.user_auth import user_auth
from src.infra.database.factory import get_async_provider, get_sync_provider

auth = Authentication()


class UserIDs(BaseModel):
    """A Pydantic model to hold user and organisation IDs for type safety."""

    user_id: UUID
    organisation_id: UUID


def get_current_user(user: User = Depends(user_auth)) -> UserIDs:
    """FastAPI dependency to extract user_id and organisation_id from the authenticated user."""
    if not user or not user.user_id or not user.organisation_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing user credentials.",
        )
    return UserIDs(user_id=user.user_id, organisation_id=user.organisation_id)


# ---- SYNC ----


def _sync_org_session_gen(organisation_id: UUID) -> Iterator[Session]:
    """Generator function that yields a synchronous organisation database session."""
    provider = get_sync_provider()
    with provider.tenant_session(str(organisation_id)) as session:  # type: ignore
        yield session


def get_org_session(user_ids: UserIDs = Depends(get_current_user)) -> Iterator[Session]:
    """FastAPI dependency yielding an organisation-scoped Session.

    Use in endpoint signatures: `session: Session = Depends(get_org_session)`.
    """
    yield from _sync_org_session_gen(user_ids.organisation_id)


def acquire_org_session(organisation_id: UUID) -> Iterator[Session]:
    """Imperative helper for sync code outside FastAPI dependency system."""
    return _sync_org_session_gen(organisation_id)


# ---- ASYNC ----


async def _async_org_session_gen(organisation_id: UUID) -> AsyncIterator[AsyncSession]:
    """Generator function that yields an asynchronous organisation database session."""
    provider = get_async_provider()
    async with provider.tenant_session(str(organisation_id)) as session:  # type: ignore
        yield session


async def get_org_async_session(
    user_ids: UserIDs = Depends(get_current_user),
) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding an async organisation-scoped Session.

    Use in endpoint signatures: `session: AsyncSession = Depends(get_org_async_session)`.
    """
    async for s in _async_org_session_gen(user_ids.organisation_id):
        yield s


async def acquire_org_async_session(
    organisation_id: UUID,
) -> AsyncIterator[AsyncSession]:
    """Imperative helper for async code outside FastAPI dependency system."""
    async for s in _async_org_session_gen(organisation_id):
        yield s


# ---- __all__ ----

__all__ = [
    "get_org_session",
    "acquire_org_session",
    "get_org_async_session",
    "acquire_org_async_session",
    "UserIDs",
    "get_current_user",
]
