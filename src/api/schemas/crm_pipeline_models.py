"""CRM pipeline and stage response models."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field
from src.api.schemas.crm_settings_models import CRMSettingsLastChanged


class PipelineStageItem(BaseModel):
    """Pipeline stage details."""

    stage_id: UUID
    name: str
    label: str | None = None
    stage_state: str
    probability: float | None = None
    is_closed: bool
    display_order: int | None = None
    updated_at: datetime
    deal_count: int | None = None
    last_changed: CRMSettingsLastChanged | None = None

    model_config = {"from_attributes": True}


class PipelineItem(BaseModel):
    """Pipeline summary with optional stages."""

    pipeline_id: UUID
    name: str
    label: str | None = None
    is_default: bool
    is_active: bool
    display_order: int | None = None
    updated_at: datetime
    deal_count: int | None = None
    last_changed: CRMSettingsLastChanged | None = None
    stages: list[PipelineStageItem] = Field(default_factory=list)

    model_config = {"from_attributes": True}
