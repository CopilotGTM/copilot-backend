"""CRM deal participant request/response models."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from src.domain.models.organisation_tables.sync.crm.deals import CRMParticipantType


class _StrictParticipantWriteModel(BaseModel):
    """Base class for participant write payloads that rejects unknown fields."""

    model_config = ConfigDict(extra="forbid")


class DealParticipantCreateItem(_StrictParticipantWriteModel):
    """Participant entry to attach to a deal."""

    participant_type: CRMParticipantType
    related_id: UUID
    relationship_role: str | None = None

    @field_validator("relationship_role")
    @classmethod
    def normalize_role(cls, v: str | None) -> str | None:
        """Trim optional relationship roles before creating participant rows."""
        if v is None:
            return None
        value = v.strip()
        return value or None


class DealParticipantsCreateRequest(_StrictParticipantWriteModel):
    """Request payload to add participants to a deal."""

    participants: list[DealParticipantCreateItem] = Field(min_length=1)


class DealParticipantUpdateRequest(_StrictParticipantWriteModel):
    """Request payload to update a participant role."""

    relationship_role: str | None = None

    @field_validator("relationship_role")
    @classmethod
    def normalize_role(cls, v: str | None) -> str | None:
        """Trim role updates while preserving explicit null clears."""
        if v is None:
            return None
        value = v.strip()
        return value or None


class DealParticipantItem(BaseModel):
    """Participant summary returned by write endpoints."""

    participant_id: UUID
    deal_id: UUID
    participant_type: str
    related_local_id: UUID | None = None
    related_name: str | None = None
    related_email: str | None = None
    linkedin_url: str | None = None
    relationship_role: str | None = None
    is_deleted: bool

    model_config = {"from_attributes": True}


class DealParticipantSkippedItem(BaseModel):
    """Skipped participant with a reason."""

    participant_type: str
    related_id: UUID
    relationship_role: str | None = None
    reason: str


class DealParticipantsWriteResponse(BaseModel):
    """Response payload for participant creation."""

    created: list[DealParticipantItem]
    skipped: list[DealParticipantSkippedItem]
    total_requested: int
    total_created: int
    total_skipped: int
