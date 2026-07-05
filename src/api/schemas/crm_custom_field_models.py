"""API models for CRM custom field definitions."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from src.api.schemas.crm_settings_models import CRMSettingsLastChanged
from src.domain.models.organisation_tables.sync.crm.custom_fields import (
    CRMCustomFieldDataType,
    CRMCustomFieldEntity,
)


class CustomFieldDefinitionItem(BaseModel):
    """Custom field definition exposed to frontend clients."""

    field_id: UUID
    entity: CRMCustomFieldEntity
    key: str
    label: str
    data_type: CRMCustomFieldDataType
    required: bool = False
    is_predefined: bool = False
    is_active: bool = True
    display_order: int | None = None
    description: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    validation: dict[str, Any] = Field(default_factory=dict)
    formula: dict[str, Any] | None = None
    usage_count: int | None = None
    last_changed: CRMSettingsLastChanged | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CreateCustomFieldRequest(BaseModel):
    """Request payload for creating a custom field definition."""

    entity: CRMCustomFieldEntity = CRMCustomFieldEntity.DEAL
    key: str = Field(
        ...,
        min_length=2,
        max_length=63,
        description="Machine key. Accepts custom_* from CSV and stores it without prefix.",
    )
    label: str = Field(..., min_length=1, max_length=255)
    data_type: CRMCustomFieldDataType = CRMCustomFieldDataType.TEXT
    required: bool = False
    is_predefined: bool = False
    display_order: int | None = None
    description: str | None = Field(None, max_length=500)
    options: dict[str, Any] | None = None
    validation: dict[str, Any] | None = None
    formula: dict[str, Any] | None = None


class UpdateCustomFieldRequest(BaseModel):
    """Request payload for updating a custom field definition with concurrency."""

    label: str | None = Field(None, min_length=1, max_length=255)
    required: bool | None = None
    is_active: bool | None = None
    display_order: int | None = None
    if_updated_at: datetime
    description: str | None = Field(None, max_length=500)
    options: dict[str, Any] | None = None
    validation: dict[str, Any] | None = None
    formula: dict[str, Any] | None = None


class ReorderCustomFieldsRequest(BaseModel):
    """Atomic custom-column reorder request for one entity."""

    entity: CRMCustomFieldEntity = CRMCustomFieldEntity.DEAL
    field_ids: list[UUID] = Field(min_length=1)
    if_updated_at: datetime


class RestoreCustomFieldRequest(BaseModel):
    """Restore an archived custom column with optimistic concurrency."""

    if_updated_at: datetime


class ClearCustomFieldValuesResponse(BaseModel):
    """Result of clearing one archived custom column from deals."""

    cleared_deal_count: int


class ValidateFormulaRequest(BaseModel):
    """Request payload for validating a formula expression."""

    entity: CRMCustomFieldEntity = CRMCustomFieldEntity.DEAL
    formula: dict[str, Any]


class ValidateFormulaResponse(BaseModel):
    """Validation result for a formula expression."""

    is_valid: bool
    dependencies: list[str] = Field(default_factory=list)
