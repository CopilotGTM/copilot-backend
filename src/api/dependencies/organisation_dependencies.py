"""Organisation-level database session dependencies (sync + async).

FastAPI-specific dependencies that wrap the core session functions from infra.
Use get_org_session/get_org_async_session in FastAPI endpoints with authentication.

For non-FastAPI code (workers, services), import directly from:
    from src.infra.database.sessions import acquire_org_session, acquire_org_async_session
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from src.domain.models.authentication import Authentication
from src.domain.models.master_tables import User
from src.infra.auth.user_auth import user_auth

# Import core functions from infra (no FastAPI dependencies)
from src.infra.auth.user_context import UserIDs as UserIDs
from src.infra.database.sessions import (
    acquire_org_async_session as _acquire_org_async_session,
)
from src.infra.database.sessions import (
    acquire_org_session as _acquire_org_session,
)
from src.infra.utils.timezone import (
    USER_TIMEZONE_HEADER,
    resolve_timezone_name,
    validate_timezone_name,
)

# Backward compatibility alias used by legacy imports and test bootstrap.
# Keep this object exposing verify_session for older dependency overrides.
auth = Authentication()


def get_current_user(request: Request, user: User = Depends(user_auth)) -> UserIDs:
    """FastAPI dependency to extract user_id and organisation_id from the authenticated user."""
    if not user or not user.user_id or not user.organisation_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing user credentials.",
        )

    # Build user display name from first and last name
    user_name = None
    if user.first_name or user.last_name:
        user_name = f"{user.first_name or ''} {user.last_name or ''}".strip()

    header_timezone = validate_timezone_name(request.headers.get(USER_TIMEZONE_HEADER))
    effective_timezone = resolve_timezone_name(header_timezone, user.timezone, default=None)
    request.state.user_timezone = effective_timezone

    return UserIDs(
        user_id=user.user_id,
        organisation_id=user.organisation_id,
        user_name=user_name,
        user_email=str(user.email) if user.email else None,
        user_timezone=effective_timezone,
        user_role=user.user_role,
    )


# ---- SYNC FastAPI Dependencies ----


def get_org_session(user_ids: UserIDs = Depends(get_current_user)) -> Iterator[Session]:
    """FastAPI dependency yielding an organisation-scoped Session.

    Use in endpoint signatures: `session: Session = Depends(get_org_session)`.
    """
    yield from _acquire_org_session(user_ids.organisation_id)


# ---- ASYNC FastAPI Dependencies ----


async def get_org_async_session(
    user_ids: UserIDs = Depends(get_current_user),
) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding an async organisation-scoped Session.

    Use in endpoint signatures: `session: AsyncSession = Depends(get_org_async_session)`.
    """
    async for s in _acquire_org_async_session(user_ids.organisation_id):
        yield s


# ---- Re-exports for backwards compatibility ----
# NOTE: For new code, import from src.infra.database.sessions instead


def acquire_org_session(organisation_id: UUID) -> Iterator[Session]:
    """Acquire a sync org session. For new code, use src.infra.database.sessions."""
    return _acquire_org_session(organisation_id)


async def acquire_org_async_session(
    organisation_id: UUID,
) -> AsyncGenerator[AsyncSession, None]:
    """Acquire an async org session. For new code, use src.infra.database.sessions."""
    async for s in _acquire_org_async_session(organisation_id):
        yield s


__all__ = [
    "auth",
    "get_org_session",
    "acquire_org_session",
    "get_org_async_session",
    "acquire_org_async_session",
    "UserIDs",
    "get_current_user",
]
