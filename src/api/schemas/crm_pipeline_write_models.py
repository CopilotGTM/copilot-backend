"""Request models for CRM pipeline write endpoints (create / update / delete)."""

from __future__ import annotations

from typing import Literal

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


class UpdatePipelineRequest(BaseModel):
    """Partial update for a pipeline. Unset fields are untouched."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    label: str | None = Field(default=None, max_length=255)
    is_default: bool | None = None
    display_order: int | None = None

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
    """Partial update for a stage. stage_state / is_closed are immutable."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    label: str | None = Field(default=None, max_length=255)
    probability: float | None = Field(default=None, ge=0, le=100)
    display_order: int | None = None

    @field_validator("name", "label")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None
