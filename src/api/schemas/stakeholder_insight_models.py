"""Pydantic schemas for Stakeholder Insight API endpoints."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field
from src.api.schemas.email_name_fallback import EmailNameFallbackModel
from src.domain.models.organisation_tables.sync.stakeholder_insight_models import (
    StakeholderInsightPriority,
    StakeholderInsightStatus,
    StakeholderInsightType,
)

# ─────────────────────────────────────────────────────────────────────────────
# Action Models
# ─────────────────────────────────────────────────────────────────────────────


class InsightAction(BaseModel):
    """Model for a suggested action on an insight.

    Frontend handles icons and routing based on action_type.
    """

    action_type: str  # InsightActionType value as string
    label: str
    parameters: dict[str, Any] = Field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# Response Models
# ─────────────────────────────────────────────────────────────────────────────


class StakeholderInsightResponse(EmailNameFallbackModel):
    """Response model for a stakeholder insight.

    Matches the UI card structure shown in the AI Insights panel.
    """

    insight_id: UUID
    insight_type: StakeholderInsightType
    priority: StakeholderInsightPriority
    status: StakeholderInsightStatus

    # Content
    title: str
    description: str | None = None
    ai_reasoning: str

    # Stakeholder context
    stakeholder_id: UUID | None = None
    stakeholder_name: str | None = None
    stakeholder_title: str | None = None
    stakeholder_email: str | None = None

    # Actions
    actions: list[InsightAction] = Field(default_factory=list)

    # Metrics
    metric_value: float | None = None
    metric_unit: str | None = None
    threshold_value: float | None = None
    trend_direction: str | None = None

    # AI metadata
    ai_confidence: float = 0.8

    # Source
    source_type: str | None = None
    related_risk_blocker_id: UUID | None = None

    # Timestamps
    identified_at: datetime
    expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class StakeholderInsightSummary(BaseModel):
    """Summary statistics for insights on a deal."""

    critical_count: int = 0
    high_count: int = 0
    medium_count: int = 0
    low_count: int = 0


class StakeholderInsightListResponse(BaseModel):
    """Response model for listing stakeholder insights."""

    insights: list[StakeholderInsightResponse]
    total_count: int
    summary: StakeholderInsightSummary


# ─────────────────────────────────────────────────────────────────────────────
# Request Models
# ─────────────────────────────────────────────────────────────────────────────


class ResolveInsightRequest(BaseModel):
    """Request model for resolving an insight."""

    resolution_notes: str | None = Field(
        default=None,
        max_length=2000,
        description="Notes about how the insight was addressed",
    )


__all__ = [
    "InsightAction",
    "StakeholderInsightResponse",
    "StakeholderInsightSummary",
    "StakeholderInsightListResponse",
    "ResolveInsightRequest",
]
