"""CRM user selection response models."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field


class CRMUserListItem(BaseModel):
    """App user entry eligible for CRM owner/team selection."""

    user_id: UUID
    name: str | None = None
    email: str | None = None
    role: str | None = None
    job_title: str | None = None
    organisation_role: str | None = None
    status: str | None = None

    model_config = {"from_attributes": True}


class CRMUsersPage(BaseModel):
    """Paginated list of CRM-eligible users."""

    items: list[CRMUserListItem]
    cursor: str | None = Field(default=None, description="Cursor for next page")
