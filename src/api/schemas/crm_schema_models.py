"""API models for CRM entity schema metadata."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class CRMFieldSchemaItem(BaseModel):
    """One field definition in a CRM entity schema response."""

    field_id: UUID | None = Field(None, description="Custom field definition ID, if applicable.")
    entity: str = Field(..., description="Entity scope for the field, for example deal.")
    key: str = Field(..., description="Stable API/storage key for the field.")
    label: str = Field(..., description="Display label for UI and mapping surfaces.")
    type: str = Field(..., description="Canonical value type.")
    order: int = Field(..., description="Stable display order within this schema.")
    section: str | None = Field(None, description="Logical UI section for standard fields.")
    source: str = Field(..., description="standard, internal, or provider.")
    ownership: str = Field(..., description="Field owner: system, internal, or provider name.")
    integration_id: UUID | None = Field(None, description="Provider integration ID, if any.")
    provider_name: str | None = Field(None, description="Provider name for mirrored fields.")
    provider_field_key: str | None = Field(None, description="Provider-native field key.")
    required: bool = Field(False, description="Whether internal writes must provide a value.")
    read_only: bool = Field(False, description="Whether direct field editing is blocked.")
    nullable: bool = Field(True, description="Whether an empty value is acceptable.")
    editable: bool = Field(
        False, description="Whether direct internal CRM writes can edit this field."
    )
    sortable: bool = Field(
        False, description="Whether list/reporting surfaces can sort by this field."
    )
    filterable: bool = Field(
        False, description="Whether list/reporting surfaces can filter by this field."
    )
    searchable: bool = Field(False, description="Whether keyword search can match this field.")
    input_control: str | None = Field(None, description="Suggested frontend control.")
    options: dict[str, Any] = Field(default_factory=dict, description="Type-specific options.")
    validation: dict[str, Any] = Field(
        default_factory=dict,
        description="Type-specific frontend/backend validation hints.",
    )
    lifecycle_state: str = Field("active", description="Field lifecycle state.")
    write_path: str | None = Field(None, description="Request path accepted by write APIs.")
    write_policy: str = Field("read_only", description="Write semantic for this field.")
    read_only_reason: str | None = Field(
        None, description="Why the field cannot be directly edited."
    )
    internal_write: bool = Field(
        False, description="Whether internal CRM writes accept this field."
    )
    internal_write_validation: dict[str, bool] = Field(
        default_factory=dict,
        description="Backend validation gates applied to internal writes.",
    )


class CRMEntitySchemaResponse(BaseModel):
    """Ordered field schema for one CRM entity."""

    entity: str
    fields: list[CRMFieldSchemaItem]
