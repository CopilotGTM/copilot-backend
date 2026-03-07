"""Pydantic schemas for Risk & Blocker API endpoints."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import Query
from pydantic import BaseModel, Field
from src.domain.models.organisation_tables.sync.risk_blocker_models import (
    RiskBlockerCategory,
    RiskBlockerSeverity,
    RiskBlockerSource,
    RiskBlockerStatus,
    RiskBlockerType,
)


class RiskBlockerListItem(BaseModel):
    """Response model for a risk/blocker in list view.

    Optimized for UI table display with only essential fields:
    - TITLE, TYPE, OWNER, SOURCE, STATUS columns
    """

    risk_blocker_id: UUID
    item_type: RiskBlockerType  # RISK or BLOCKER
    status: RiskBlockerStatus
    severity: RiskBlockerSeverity

    # Content (for title column)
    title: str
    quote: str | None = None  # Snippet shown in AI suggested panel

    # Source (for source column with icon)
    source_type: RiskBlockerSource
    source_record_id: str | None = None
    source_context: str | None = None  # e.g., "Discovery Call - Dec 15"

    # Assignment (for owner column)
    owner_name: str | None = None

    # AI metadata (for AI suggested badge)
    is_ai_generated: bool

    model_config = {"from_attributes": True}


class RiskBlockerDetail(BaseModel):
    """Response model for full risk/blocker details including audit trail."""

    risk_blocker_id: UUID
    deal_id: UUID
    item_type: RiskBlockerType
    category: RiskBlockerCategory
    severity: RiskBlockerSeverity
    status: RiskBlockerStatus

    # Content
    title: str
    description: str | None = None
    quote: str | None = None

    # Source
    source_type: RiskBlockerSource
    source_record_id: str | None = None
    source_context: str | None = None

    # AI metadata
    is_ai_generated: bool
    ai_confidence: float | None = None
    ai_reasoning: str | None = None
    extraction_run_id: UUID | None = None

    # Assignment
    owner_name: str | None = None
    owner_email: str | None = None
    assigned_team: str | None = None

    # Resolution
    resolution_notes: str | None = None
    resolved_at: datetime | None = None
    resolved_by: str | None = None

    # Provenance
    created_by_user_id: UUID | None = None
    created_by_user_name: str | None = None
    last_modified_by_user_id: UUID | None = None
    last_modified_by_user_name: str | None = None
    user_confirmed_at: datetime | None = None

    # Timestamps
    identified_at: datetime
    created_at: datetime
    updated_at: datetime

    # Full audit trail
    audit_trail: list[dict] = Field(default_factory=list)
    modification_count: int = 0
    unique_modifier_count: int = 0

    model_config = {"from_attributes": True}


class RiskBlockerSummary(BaseModel):
    """Summary statistics for risks/blockers on a deal."""

    total_count: int = 0
    risks_count: int = 0
    blockers_count: int = 0
    ai_suggested_count: int = 0
    open_count: int = 0
    in_progress_count: int = 0
    resolved_count: int = 0
    critical_count: int = 0
    high_count: int = 0


class RiskBlockerListResponse(BaseModel):
    """Response model for paginated list of risks/blockers."""

    deal_id: UUID
    items: list[RiskBlockerListItem]
    page: int
    limit: int
    total_count: int
    total_pages: int
    summary: RiskBlockerSummary


class RiskBlockerCreateRequest(BaseModel):
    """Request model for creating a risk/blocker manually."""

    item_type: RiskBlockerType = Field(..., description="RISK or BLOCKER")
    title: str = Field(..., max_length=500, description="Short title")
    description: str | None = Field(None, description="Detailed description")
    category: RiskBlockerCategory = Field(
        default=RiskBlockerCategory.OTHER,
        description="Category of the risk/blocker",
    )
    severity: RiskBlockerSeverity = Field(
        default=RiskBlockerSeverity.MEDIUM,
        description="Severity level",
    )
    owner_name: str | None = Field(None, max_length=255, description="Assigned owner")
    owner_email: str | None = Field(None, max_length=255, description="Owner email")
    assigned_team: str | None = Field(None, max_length=128, description="Assigned team")


class RiskBlockerUpdateRequest(BaseModel):
    """Request model for updating a risk/blocker."""

    item_type: RiskBlockerType | None = None
    title: str | None = Field(None, max_length=500)
    description: str | None = None
    category: RiskBlockerCategory | None = None
    severity: RiskBlockerSeverity | None = None
    status: RiskBlockerStatus | None = None
    owner_name: str | None = None
    owner_email: str | None = None
    assigned_team: str | None = None


class RiskBlockerResolveRequest(BaseModel):
    """Request model for resolving a risk/blocker."""

    resolution_notes: str = Field(
        ...,
        min_length=10,
        description="Notes on how the issue was resolved",
    )


class RiskBlockerListQueryParams(BaseModel):
    """Query parameters for listing risks and blockers."""

    limit: int = Query(20, ge=1, le=100, description="Number of items per page")
    page: int = Query(1, ge=1, description="Page number (1-indexed)")
    item_type: RiskBlockerType | None = Query(None, description="Filter by RISK or BLOCKER")
    status: str | None = Query(
        None,
        description="Comma-separated statuses to filter (OPEN,IN_PROGRESS,RESOLVED)",
    )
    severity: RiskBlockerSeverity | None = Query(None, description="Filter by severity")
    category: RiskBlockerCategory | None = Query(None, description="Filter by category")
    include_resolved: bool = Query(False, description="Include resolved/dismissed items")
    sort: str = Query("desc", pattern="^(asc|desc)$", description="Sort order")
