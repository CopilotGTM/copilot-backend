from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class CRMImportPresignRequest(BaseModel):
    filename: str = Field(min_length=1)
    mode: str = "account_contact"
    update_existing: bool = False


class CRMImportPresignResponse(BaseModel):
    import_id: UUID
    upload_url: str
    storage_path: str


class CRMImportMappingRequest(BaseModel):
    mapping: dict[str, str]
    update_existing: bool = False


class CRMImportStatusResponse(BaseModel):
    import_id: UUID
    status: str
    crm_import_status: str | None = None
    filename: str | None = None
    storage_path: str | None = None
    analysis: dict[str, Any] | None = None
    mapping: dict[str, str] = Field(default_factory=dict)
    mapping_suggestions: list[dict[str, Any]] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    percent_complete: float = 0.0
    latest_errors: list[dict[str, Any]] = Field(default_factory=list)
    mapping_confidence_summary: dict[str, int] = Field(default_factory=dict)


class CRMImportRowItem(BaseModel):
    row_number: int
    status: str
    target_type: str | None = None
    account_id: UUID | None = None
    contact_id: UUID | None = None
    deal_id: UUID | None = None
    error_code: str | None = None
    error_message: str | None = None
    raw_values: dict[str, Any] = Field(default_factory=dict)
    normalized_values: dict[str, Any] = Field(default_factory=dict)


class CRMImportRowsResponse(BaseModel):
    items: list[CRMImportRowItem]
    next_cursor: int | None = None
    has_more: bool = False
