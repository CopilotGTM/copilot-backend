"""Request and response contracts for audited CRM CSV imports."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class CRMImportPresignRequest(BaseModel):
    """Upload request with the pipeline defaults needed before analysis."""

    filename: str = Field(min_length=1, max_length=255)
    mode: Literal["account_contact_deal"] = "account_contact_deal"
    update_existing: bool = False
    pipeline_id: UUID
    default_stage_id: UUID
    default_owner_user_id: UUID | None


class CRMImportPresignResponse(BaseModel):
    """Scoped storage location for a newly created import."""

    import_id: UUID
    upload_url: str
    storage_path: str


class CRMImportMappingRequest(BaseModel):
    """Complete header mapping plus mutable import configuration."""

    mapping: dict[str, str] = Field(max_length=200)
    ignored_columns: list[str] = Field(default_factory=list, max_length=200)
    column_formats: dict[str, Literal["DMY", "MDY"]] = Field(
        default_factory=dict,
        max_length=200,
    )
    pipeline_id: UUID
    default_stage_id: UUID
    default_owner_user_id: UUID | None
    update_existing: bool = False
    retry_failed_rows: bool = False


class CRMImportTargetItem(BaseModel):
    """One backend-owned writable mapping target."""

    target_field: str
    label: str
    group: Literal["account", "contact", "deal"]
    value_type: str
    required: bool = False
    read_only: bool = False
    custom_field_id: UUID | None = None
    custom_field_key: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    validation: dict[str, Any] = Field(default_factory=dict)


class CRMImportStageItem(BaseModel):
    """One active stage in the selected internal pipeline."""

    stage_id: UUID
    stage_key: str | None = None
    name: str
    label: str | None = None
    stage_state: str
    probability: float | None = None


class CRMImportTargetsResponse(BaseModel):
    """Pipeline-scoped mapping catalog used by analysis and the frontend."""

    pipeline_id: UUID
    pipeline_name: str
    targets: list[CRMImportTargetItem]
    stages: list[CRMImportStageItem]


class CRMImportStatusResponse(BaseModel):
    """Refresh-safe import state, configuration, suggestions, and counts."""

    import_id: UUID
    status: str
    crm_import_status: str | None = None
    filename: str | None = None
    storage_path: str | None = None
    analysis: dict[str, Any] | None = None
    mapping: dict[str, str] = Field(default_factory=dict)
    ignored_columns: list[str] = Field(default_factory=list)
    column_formats: dict[str, str] = Field(default_factory=dict)
    pipeline_id: UUID | None = None
    default_stage_id: UUID | None = None
    default_owner_user_id: UUID | None = None
    update_existing: bool = False
    mapping_suggestions: list[dict[str, Any]] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    percent_complete: float = 0.0
    latest_errors: list[dict[str, Any]] = Field(default_factory=list)
    mapping_confidence_summary: dict[str, int] = Field(default_factory=dict)


class CRMImportRowItem(BaseModel):
    """Audited outcome and presentation snapshot for one CSV row."""

    row_number: int
    status: str
    deal_name: str | None = None
    account_name: str | None = None
    contact_name: str | None = None
    contact_email: str | None = None
    owner_name: str | None = None
    stage_name: str | None = None
    target_type: str | None = None
    account_id: UUID | None = None
    contact_id: UUID | None = None
    deal_id: UUID | None = None
    owner_user_id: UUID | None = None
    stage_id: UUID | None = None
    account_outcome: str | None = None
    contact_outcome: str | None = None
    attempt_count: int = 0
    error_code: str | None = None
    error_field: str | None = None
    error_message: str | None = None
    raw_values: dict[str, Any] = Field(default_factory=dict)
    normalized_values: dict[str, Any] = Field(default_factory=dict)


class CRMImportRowsResponse(BaseModel):
    """Cursor page of durable row outcomes."""

    items: list[CRMImportRowItem]
    next_cursor: int | None = None
    has_more: bool = False
