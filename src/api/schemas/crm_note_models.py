"""CRM API response models for deal notes."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class NoteAuthor(BaseModel):
    """Note author information."""

    id: str = Field(description="User ID or CRM user ID")
    name: str | None = Field(default=None, description="Author name")
    email: str | None = Field(default=None, description="Author email")


class NoteMetadata(BaseModel):
    """Additional note metadata for CRM-synced notes."""

    external_id: str | None = Field(default=None, description="CRM external ID if synced")
    synced_at: datetime | None = Field(default=None, description="Last sync time if CRM note")


class DealParticipant(BaseModel):
    """Participant associated with a deal (stakeholder or owner).

    Used to provide email suggestions for @mentions or auto-complete.
    """

    email: str = Field(description="Participant email address")
    name: str | None = Field(default=None, description="Participant name")


class DealNoteItem(BaseModel):
    """Individual note item in a list response."""

    note_id: UUID = Field(description="Internal note UUID")
    deal_id: UUID = Field(description="Deal ID this note belongs to")
    content: str = Field(description="Note content/body")
    subject: str | None = Field(default=None, description="Note subject/title")
    created_at: datetime = Field(description="When note was created")
    updated_at: datetime | None = Field(default=None, description="When note was last updated")
    source: str = Field(description="Note source: user, hubspot, salesforce")
    is_user_created: bool = Field(description="True if created by user, not CRM")
    author: NoteAuthor = Field(description="Note author information")
    metadata: NoteMetadata | None = Field(default=None, description="Additional metadata")

    model_config = {"from_attributes": True}


class DealNotesPage(BaseModel):
    """Paginated list of deal notes."""

    items: list[DealNoteItem] = Field(description="List of notes")
    cursor: str | None = Field(
        default=None,
        description="Cursor for next page; null if no more results",
    )
    total_count: int = Field(description="Total number of notes for this deal")
    participants: list[DealParticipant] = Field(
        default_factory=list,
        description="List of deal participants (owner + stakeholders) for @mentions",
    )


class CreateNoteRequest(BaseModel):
    """Request payload to create a new note."""

    content: str = Field(
        min_length=1,
        max_length=10000,
        description="Note content (required, max 10000 characters)",
    )
    subject: str | None = Field(
        default=None,
        max_length=500,
        description="Note subject/title (optional, max 500 characters)",
    )

    @field_validator("content")
    @classmethod
    def content_not_empty(cls, v: str) -> str:
        """Validate content is not empty or whitespace-only."""
        if not v or not v.strip():
            msg = "Content cannot be empty or whitespace-only"
            raise ValueError(msg)
        return v.strip()

    @field_validator("subject")
    @classmethod
    def subject_strip_whitespace(cls, v: str | None) -> str | None:
        """Strip whitespace from subject if provided."""
        if v is not None:
            v = v.strip()
            return v if v else None
        return None
