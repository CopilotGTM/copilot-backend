"""CRM deal write request/response models."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class DealCreateRequest(BaseModel):
    """Request payload to create an internal CRM deal."""

    name: str = Field(min_length=1, max_length=500)
    amount: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    pipeline_id: UUID | None = None
    stage_id: UUID | None = None
    owner_user_id: UUID | None = None
    close_date: datetime | None = None
    expected_close_date: datetime | None = None
    description: str | None = None
    deal_type: str | None = None
    priority: str | None = None
    lead_source: str | None = None

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        value = v.strip()
        if not value:
            raise ValueError("Deal name cannot be empty")
        return value

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return v.strip().upper() or None


class DealUpdateRequest(BaseModel):
    """Request payload to update an internal CRM deal."""

    name: str | None = Field(default=None, max_length=500)
    amount: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    close_date: datetime | None = None
    expected_close_date: datetime | None = None
    description: str | None = None
    deal_type: str | None = None
    priority: str | None = None
    lead_source: str | None = None

    @field_validator("name")
    @classmethod
    def normalize_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        value = v.strip()
        return value or None

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return v.strip().upper() or None


class DealStageMoveRequest(BaseModel):
    """Request payload to move a deal to a new stage."""

    stage_id: UUID


class DealOwnerAssignRequest(BaseModel):
    """Request payload to assign or clear a deal owner."""

    owner_user_id: UUID | None = None


class DealWriteResponse(BaseModel):
    """Minimal response for deal write operations."""

    deal_id: UUID
    external_id: str
    name: str | None
    pipeline_id: UUID | None
    stage_id: UUID | None
    deal_status: str
    owner_user_id: UUID | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
