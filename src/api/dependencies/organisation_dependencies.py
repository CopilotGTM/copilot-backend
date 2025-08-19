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
from src.infra.utils.database import get_org_session as _get_org_session
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
    """
    FastAPI dependency that provides an organisation-specific database session
    by extracting the organisation_id from the current authenticated user.
    """
    yield from _get_org_session(str(user_ids.organisation_id))


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