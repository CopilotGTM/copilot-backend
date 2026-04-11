"""User context models for services and workers.

These models are used to pass user identity context through
the application without depending on FastAPI.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel


class UserIDs(BaseModel):
    """A Pydantic model to hold user and organisation IDs for type safety."""

    user_id: UUID
    organisation_id: UUID
    user_name: str | None = None
    user_email: str | None = None
    user_timezone: str | None = None


__all__ = ["UserIDs"]
