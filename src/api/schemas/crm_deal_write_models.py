"""API-facing CRM deal write schemas.

Request models are re-exported from shared CRM contracts so agent and worker
code can validate the same payloads without importing the API package.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field
from src.api.schemas.crm_models import DealJobResponse, DealOwnerDetail, DealPrimaryContactDetail
from src.contracts.crm.deal_write import (
    DealAccountUpdateRequest,
    DealCloseRequest,
    DealCreateRequest,
    DealOwnerAssignRequest,
    DealPrimaryContactUpdateRequest,
    DealReopenRequest,
    DealStageMoveRequest,
    DealUpdateRequest,
)

__all__ = [
    "DealAccountUpdateRequest",
    "DealCloseRequest",
    "DealCreateRequest",
    "DealOwnerAssignRequest",
    "DealPrimaryContactUpdateRequest",
    "DealReopenRequest",
    "DealStageMoveRequest",
    "DealUpdateRequest",
    "DealWriteResponse",
]


class DealWriteResponse(BaseModel):
    """Minimal response for deal write operations."""

    deal_id: UUID
    name: str | None
    amount: float | None = None
    predicted_amount: float | None = None
    currency: str | None = None
    currency_symbol: str | None = None
    pipeline_id: UUID | None
    pipeline_name: str | None = None
    pipeline_label: str | None = None
    stage_id: UUID | None
    stage_name: str | None = None
    stage_label: str | None = None
    deal_status: str
    probability: float | None = None
    forecast_category: str | None = None
    owner_user_id: UUID | None
    owner_name: str | None = None
    owner_email: str | None = None
    owner_details: list[DealOwnerDetail] = Field(default_factory=list)
    is_internal: bool = True
    company_id: UUID | None = None
    company_name: str | None = None
    company_domain: str | None = None
    company_industry: str | None = None
    company_website: str | None = None
    company_linkedin_url: str | None = None
    company_country: str | None = None
    primary_contact_id: UUID | None = None
    primary_contact_name: str | None = None
    primary_contact_email: str | None = None
    primary_contact_details: list[DealPrimaryContactDetail] = Field(default_factory=list)
    processing_jobs: list[DealJobResponse] = Field(default_factory=list)
    source_created_at: datetime | None = None
    close_date: datetime | None = None
    expected_close_date: datetime | None = None
    closed_won_at: datetime | None = None
    closed_lost_at: datetime | None = None
    closed_reason: str | None = None
    stage_entered_at: datetime | None = None
    stage_updated_at: datetime | None = None
    deal_type: str | None = None
    priority: str | None = None
    lead_source: str | None = None
    description: str | None = None
    created_at: datetime
    updated_at: datetime
    synced_at: datetime | None = None

    model_config = {"from_attributes": True}
