"""CRM API response models for deals and related entities."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class DealListItem(BaseModel):
    """Individual deal item in a list response."""

    deal_id: UUID
    external_id: str
    name: str | None
    amount: float | None
    currency: str | None
    deal_status: str
    probability: float | None
    close_date: datetime | None
    stage_name: str | None
    stage_label: str | None
    pipeline_name: str | None
    pipeline_label: str | None
    owner_name: str | None
    owner_email: str | None
    company_external_id: str | None
    company_name: str | None = None
    company_domain: str | None = None
    primary_contact_external_id: str | None
    primary_contact_name: str | None = None
    primary_contact_email: str | None = None
    created_at: datetime
    updated_at: datetime
    synced_at: datetime

    model_config = {"from_attributes": True}


class DealsPage(BaseModel):
    """Paginated list of deals."""

    items: list[DealListItem]
    cursor: str | None = Field(None, description="Cursor for next page; null if no more results")


class DealDetail(BaseModel):
    """Detailed deal information including all available fields."""

    # Identity
    deal_id: UUID
    external_id: str

    # Core business context
    name: str | None
    amount: float | None
    currency: str | None
    amount_home_currency: float | None
    deal_status: str
    probability: float | None
    predicted_amount: float | None
    close_date: datetime | None
    expected_close_date: datetime | None
    closed_won_at: datetime | None
    closed_lost_at: datetime | None
    closed_reason: str | None

    # Sales process snapshot
    stage_external_id: str | None
    stage_name: str | None
    stage_label: str | None
    pipeline_external_id: str | None
    pipeline_name: str | None
    pipeline_label: str | None
    pipeline_id: UUID | None
    stage_id: UUID | None
    forecast_category: str | None
    deal_type: str | None
    priority: str | None
    lead_source: str | None

    # Ownership & associations
    owner_external_id: str | None
    owner_name: str | None
    owner_email: str | None
    company_external_id: str | None
    company_name: str | None = None
    company_domain: str | None = None
    primary_contact_external_id: str | None
    primary_contact_name: str | None = None
    primary_contact_email: str | None = None

    # Activity & timing
    source_created_at: datetime | None
    last_activity_at: datetime | None
    stage_entered_at: datetime | None
    stage_updated_at: datetime | None
    synced_at: datetime
    source_modified_at: datetime | None
    last_seen_at: datetime | None

    # Freeform details
    description: str | None

    # Integration metadata
    integration_id: UUID
    organisation_id: UUID

    # Timestamps
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DealsStats(BaseModel):
    """Aggregate statistics for deals."""

    total: int
    by_status: dict[str, int]
    by_pipeline: dict[str, int]
    total_amount: float | None = Field(None, description="Sum of all deal amounts")
    average_amount: float | None = Field(None, description="Average deal amount")
