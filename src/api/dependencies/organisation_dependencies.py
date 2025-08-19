from __future__ import annotations

from uuid import UUID
from typing import Dict, Any

from fastapi import Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session

from src.domain.models.authentication import Authentication
from src.infra.utils.database import get_org_session
from src.infra.utils.logger import get_logger

auth = Authentication()
logger = get_logger(__name__)


class UserIDs(BaseModel):
    """A Pydantic model to hold the user and organisation IDs for type safety."""
    user_id: UUID
    organisation_id: UUID


def get_current_user_ids(auth_data: Dict[str, Any] = Depends(auth.verify_session)) -> UserIDs:
    """
    FastAPI dependency to extract and validate user_id and organisation_id from the authentication session.

    This provides a consistent and reusable way for any endpoint to get the
    context of the currently authenticated user and their organisation.

    Raises:
        HTTPException: If the authentication payload is missing required keys or they are invalid.

    Returns:
        UserIDs: A Pydantic model containing the validated user_id and organisation_id.
    """
    try:
        user_id = UUID(auth_data["user_id"])
        organisation_id = UUID(auth_data["organisation_id"])
        return UserIDs(user_id=user_id, organisation_id=organisation_id)
    except (KeyError, ValueError) as e:
        logger.warning("Invalid authentication payload: %s", e)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing authentication credentials.",
        ) from e


class OrgServiceDependencies:
    """
    A dependency class that provides both the organisation-specific database session
    and the current user's IDs. This is a convenient way to inject common
    dependencies required by most organisation-specific services.
    """
    def __init__(
        self,
        org_session: Session = Depends(get_org_session),
        user_ids: UserIDs = Depends(get_current_user_ids),
    ):
        self.org_session = org_session
        self.user_id = user_ids.user_id
        self.organisation_id = user_ids.organisation_id