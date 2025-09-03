from __future__ import annotations

from uuid import UUID
from typing import Dict, Any

from fastapi import Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session
from typing import Iterator

from src.domain.models.authentication import Authentication
from src.domain.models.master_tables import User
from src.infra.auth.user_auth import user_auth
from src.infra.database.factory import get_sync_provider
from src.infra.utils.logger import get_logger

auth = Authentication()
logger = get_logger(__name__)


class UserIDs(BaseModel):
    """A Pydantic model to hold the user and organisation IDs for type safety."""
    user_id: UUID
    organisation_id: UUID


def get_current_user(user: User = Depends(user_auth)) -> UserIDs:
    """
    FastAPI dependency to extract user_id and organisation_id from the authenticated user.
    """
    if not user or not user.user_id or not user.organisation_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing user credentials.",
        )
    return UserIDs(user_id=user.user_id, organisation_id=user.organisation_id)


def get_org_session(user_ids: UserIDs = Depends(get_current_user)) -> Iterator[Session]:
    """FastAPI dependency yielding an organisation-scoped Session.

    Production rationale:
    - Central place to apply multi-tenant session acquisition (reduces drift).
    - Easier to override in tests (FastAPI dependency override) without touching services.
    - Hides provider implementation details from routers.
    """
    provider = get_sync_provider()  # singleton underneath
    with provider.tenant_session(str(user_ids.organisation_id)) as session:  # type: ignore
        yield session


def acquire_org_session(organisation_id: UUID) -> Iterator[Session]:
    """Imperative helper (non‑FastAPI) to acquire a tenant session.

    Use in long‑lived flows (e.g. SSE generators, background threads) where
    dependency scopes would close too early. Keeps logic colocated with
    dependency version to avoid divergence.
    """
    provider = get_sync_provider()
    with provider.tenant_session(str(organisation_id)) as session:  # type: ignore
        yield session


class OrgServiceDependencies:
    """
    A dependency class that provides both the organisation-specific database session
    and the current user's IDs. This is a convenient way to inject common
    dependencies required by most organisation-specific services.
    """
    def __init__(
        self,
        org_session: Session = Depends(get_org_session),
        user_ids: UserIDs = Depends(get_current_user),
    ):
        self.org_session = org_session
        self.user_id = user_ids.user_id
        self.organisation_id = user_ids.organisation_id