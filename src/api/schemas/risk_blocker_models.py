"""Pydantic schemas for Risk & Blocker API endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import Query
from pydantic import BaseModel, Field
from src.contracts.crm.risk_blockers import (
    RiskBlockerDetail as RiskBlockerDetail,
)
from src.contracts.crm.risk_blockers import (
    RiskBlockerListItem,
    RiskBlockerSummary,
)
from src.domain.models.organisation_tables.sync.risk_blocker_models import (
    RiskBlockerCategory,
    RiskBlockerSeverity,
    RiskBlockerStatus,
    RiskBlockerType,
)


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
