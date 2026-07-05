"""Request models for CRM pipeline write endpoints (create / update / delete)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

StageStateLiteral = Literal["open", "won", "lost"]


class StageInput(BaseModel):
    """Stage definition supplied when creating a pipeline or adding a stage."""

    name: str = Field(min_length=1, max_length=255)
    label: str | None = Field(default=None, max_length=255)
    stage_state: StageStateLiteral = "open"
    probability: float | None = Field(default=None, ge=0, le=100)
    display_order: int | None = None

    @field_validator("name", "label")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None


class CreatePipelineRequest(BaseModel):
    """Create a new pipeline, optionally with an initial list of stages."""

    name: str = Field(min_length=1, max_length=255)
    label: str | None = Field(default=None, max_length=255)
    set_as_default: bool = False
    stages: list[StageInput] | None = None

    @field_validator("name", "label")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None


class ClonePipelineRequest(BaseModel):
    """Create a new pipeline by copying another pipeline's stage shape."""

    name: str = Field(min_length=1, max_length=255)
    label: str | None = Field(default=None, max_length=255)
    set_as_default: bool = False

    @field_validator("name", "label")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None


class UpdatePipelineRequest(BaseModel):
    """Partial update for a pipeline, guarded by the loaded row version."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    label: str | None = Field(default=None, max_length=255)
    is_default: bool | None = None
    display_order: int | None = None
    if_updated_at: datetime

    @field_validator("name", "label")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None


class CreateStageRequest(StageInput):
    """Add a new stage to an existing pipeline."""


class UpdateStageRequest(BaseModel):
    """Partial stage update, guarded by the loaded row version."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    label: str | None = Field(default=None, max_length=255)
    probability: float | None = Field(default=None, ge=0, le=100)
    display_order: int | None = None
    if_updated_at: datetime

    @field_validator("name", "label")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None


class ReorderStagesRequest(BaseModel):
    """Atomic stage reorder request guarded by the loaded pipeline version."""

    stage_ids: list[UUID] = Field(min_length=1)
    if_updated_at: datetime


class MoveStageDealsRequest(BaseModel):
    """Move all deals from one stage to another stage in the same pipeline."""

    destination_stage_id: UUID


class MoveStageDealsResponse(BaseModel):
    """Result of moving deals between stages."""

    moved_deal_count: int
