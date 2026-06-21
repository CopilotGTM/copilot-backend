"""Shared risk/blocker DTOs for API, service, agent, and worker layers.

These response models intentionally live outside ``src.api`` because deal-worker
runtime images do not package the API layer.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from src.domain.models.organisation_tables.sync.risk_blocker_models import (
    RiskBlockerCategory,
    RiskBlockerSeverity,
    RiskBlockerSource,
    RiskBlockerStatus,
    RiskBlockerType,
)


class RiskBlockerListItem(BaseModel):
    """Response model for a risk/blocker in list view."""

    risk_blocker_id: UUID
    item_type: RiskBlockerType
    status: RiskBlockerStatus
    severity: RiskBlockerSeverity
    title: str
    quote: str | None = None
    source_type: RiskBlockerSource
    source_record_id: str | None = None
    source_context: str | None = None
    owner_name: str | None = None
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
    title: str
    description: str | None = None
    quote: str | None = None
    source_type: RiskBlockerSource
    source_record_id: str | None = None
    source_context: str | None = None
    is_ai_generated: bool
    ai_confidence: float | None = None
    ai_reasoning: str | None = None
    extraction_run_id: UUID | None = None
    owner_name: str | None = None
    owner_email: str | None = None
    assigned_team: str | None = None
    resolution_notes: str | None = None
    closed_at: datetime | None = None
    closed_by_user_id: UUID | None = None
    closed_by_user_name: str | None = None
    created_by_user_id: UUID | None = None
    created_by_user_name: str | None = None
    last_modified_by_user_id: UUID | None = None
    last_modified_by_user_name: str | None = None
    identified_at: datetime
    created_at: datetime
    updated_at: datetime
    audit_trail: list[dict] = Field(default_factory=list)
    modification_count: int = 0
    unique_modifier_count: int = 0

    model_config = {"from_attributes": True}


class RiskBlockerSummary(BaseModel):
    """Summary statistics for risks/blockers on a deal."""

    total_count: int = 0
    risks_count: int = 0
    blockers_count: int = 0
    open_count: int = 0
    in_progress_count: int = 0
    resolved_count: int = 0
    critical_count: int = 0
    high_count: int = 0
