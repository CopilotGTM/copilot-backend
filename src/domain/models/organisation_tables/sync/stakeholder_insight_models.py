"""Stakeholder AI insights models for deal-level stakeholder recommendations.

This module provides models for AI-generated insights about stakeholders, including:
- Engagement gap detection (decision makers not contacted recently)
- Sentiment decline detection (champions with dropping engagement)
- Champion opportunity identification
- Unaddressed concern alerts (linked to risk blockers)
- Coverage gap detection (missing key departments)

Key features:
- Actions as JSONB array for flexible action types
- AI reasoning field explaining why each insight was generated
- Integration with stakeholder analysis service
- Status lifecycle management
"""

from datetime import datetime
from enum import Enum
from typing import Any, TypedDict
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, Float, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from ..base_mixins import utcnow

EMPTY_JSONB_SQL = "'{}'::jsonb"
EMPTY_JSONB_ARRAY_SQL = "'[]'::jsonb"


class StakeholderInsightType(str, Enum):
    """Types of AI-generated stakeholder insights."""

    ENGAGEMENT_GAP = "ENGAGEMENT_GAP"  # Key person not contacted recently
    SENTIMENT_DECLINING = "SENTIMENT_DECLINING"  # Champion/supporter sentiment dropping
    CHAMPION_OPPORTUNITY = "CHAMPION_OPPORTUNITY"  # Potential new champion identified
    UNADDRESSED_CONCERN = "UNADDRESSED_CONCERN"  # Risk/blocker not followed up
    STAKEHOLDER_RISK = "STAKEHOLDER_RISK"  # Champion leaving, role change, stance shift
    MISSING_DECISION_MAKER = "MISSING_DECISION_MAKER"  # No decision maker identified
    COVERAGE_GAP = "COVERAGE_GAP"  # Key department not covered


class StakeholderInsightStatus(str, Enum):
    """Lifecycle status of the insight."""

    ACTIVE = "ACTIVE"  # Currently relevant
    ACKNOWLEDGED = "ACKNOWLEDGED"  # User saw it
    RESOLVED = "RESOLVED"  # Issue addressed
    DISMISSED = "DISMISSED"  # User dismissed as not relevant
    EXPIRED = "EXPIRED"  # No longer relevant (stale)


class StakeholderInsightPriority(str, Enum):
    """Priority level of the insight."""

    CRITICAL = "CRITICAL"  # Red - immediate action needed
    HIGH = "HIGH"  # Orange - attention required
    MEDIUM = "MEDIUM"  # Yellow - worth reviewing
    LOW = "LOW"  # Blue - informational


class InsightActionType(str, Enum):
    """Action types for stakeholder insights.

    Backend defines these action types and labels.
    Frontend handles icons and routing based on action_type.
    """

    SCHEDULE_CALL = "SCHEDULE_CALL"  # Schedule a call with stakeholder
    SEND_EMAIL = "SEND_EMAIL"  # Draft/send an email
    VIEW_DETAILS = "VIEW_DETAILS"  # View detailed information
    VIEW_PROFILE = "VIEW_PROFILE"  # View stakeholder profile
    GENERATE_BRIEFING = "GENERATE_BRIEFING"  # Generate a briefing document


# Default labels for each action type
INSIGHT_ACTION_LABELS: dict[InsightActionType, str] = {
    InsightActionType.SCHEDULE_CALL: "Schedule Call",
    InsightActionType.SEND_EMAIL: "Send Email",
    InsightActionType.VIEW_DETAILS: "View Details",
    InsightActionType.VIEW_PROFILE: "View Profile",
    InsightActionType.GENERATE_BRIEFING: "Generate Briefing",
}


class InsightActionDict(TypedDict, total=False):
    """Type definition for insight action dictionaries.

    Required fields:
        action_type: The type of action (from InsightActionType enum)
        label: Display label for the action button

    Optional fields:
        parameters: Additional parameters for the action (stakeholder_id, email, etc.)
    """

    action_type: str  # InsightActionType.value
    label: str
    parameters: dict[str, Any]


def create_insight_action(
    action_type: InsightActionType,
    label: str | None = None,
    parameters: dict[str, Any] | None = None,
) -> InsightActionDict:
    """Create a properly typed insight action dictionary.

    Args:
        action_type: The type of action from InsightActionType enum
        label: Custom label (uses default from INSIGHT_ACTION_LABELS if not provided)
        parameters: Optional parameters for the action

    Returns:
        InsightActionDict with action_type, label, and parameters

    Example:
        >>> create_insight_action(InsightActionType.SCHEDULE_CALL)
        {'action_type': 'SCHEDULE_CALL', 'label': 'Schedule Call', 'parameters': {}}

        >>> create_insight_action(
        ...     InsightActionType.SEND_EMAIL,
        ...     label="Drop an Email",
        ...     parameters={"stakeholder_email": "john@example.com"}
        ... )
        {'action_type': 'SEND_EMAIL', 'label': 'Drop an Email', 'parameters': {'stakeholder_email': 'john@example.com'}}
    """
    return InsightActionDict(
        action_type=action_type.value,
        label=label or INSIGHT_ACTION_LABELS.get(action_type, action_type.value),
        parameters=parameters or {},
    )


# Pre-built action sets for common insight types
ENGAGEMENT_GAP_ACTIONS: list[InsightActionDict] = [
    create_insight_action(InsightActionType.SCHEDULE_CALL),
    create_insight_action(InsightActionType.SEND_EMAIL, label="Drop an Email"),
]

SENTIMENT_DECLINE_ACTIONS: list[InsightActionDict] = [
    create_insight_action(InsightActionType.VIEW_DETAILS),
    create_insight_action(InsightActionType.SCHEDULE_CALL),
]

CHAMPION_OPPORTUNITY_ACTIONS: list[InsightActionDict] = [
    create_insight_action(InsightActionType.VIEW_PROFILE),
    create_insight_action(InsightActionType.GENERATE_BRIEFING),
]

UNADDRESSED_CONCERN_ACTIONS: list[InsightActionDict] = [
    create_insight_action(InsightActionType.VIEW_DETAILS),
    create_insight_action(InsightActionType.GENERATE_BRIEFING),
]

STAKEHOLDER_RISK_ACTIONS: list[InsightActionDict] = [
    create_insight_action(InsightActionType.VIEW_PROFILE),
    create_insight_action(InsightActionType.SCHEDULE_CALL),
]

MISSING_DECISION_MAKER_ACTIONS: list[InsightActionDict] = [
    create_insight_action(InsightActionType.VIEW_DETAILS),
]

COVERAGE_GAP_ACTIONS: list[InsightActionDict] = [
    create_insight_action(InsightActionType.VIEW_DETAILS),
]

# Mapping from insight type to default actions
DEFAULT_ACTIONS_BY_TYPE: dict[StakeholderInsightType, list[InsightActionDict]] = {
    StakeholderInsightType.ENGAGEMENT_GAP: ENGAGEMENT_GAP_ACTIONS,
    StakeholderInsightType.SENTIMENT_DECLINING: SENTIMENT_DECLINE_ACTIONS,
    StakeholderInsightType.CHAMPION_OPPORTUNITY: CHAMPION_OPPORTUNITY_ACTIONS,
    StakeholderInsightType.UNADDRESSED_CONCERN: UNADDRESSED_CONCERN_ACTIONS,
    StakeholderInsightType.STAKEHOLDER_RISK: STAKEHOLDER_RISK_ACTIONS,
    StakeholderInsightType.MISSING_DECISION_MAKER: MISSING_DECISION_MAKER_ACTIONS,
    StakeholderInsightType.COVERAGE_GAP: COVERAGE_GAP_ACTIONS,
}


def get_default_actions(insight_type: StakeholderInsightType) -> list[InsightActionDict]:
    """Get the default actions for a given insight type.

    Args:
        insight_type: The type of insight

    Returns:
        List of default actions for this insight type
    """
    return list(DEFAULT_ACTIONS_BY_TYPE.get(insight_type, []))


class DealStakeholderInsight(SQLModel, table=True):
    """AI-generated insights about deal stakeholders.

    This table stores deal-level insights/recommendations generated by
    analyzing stakeholder data, engagement patterns, and linked risk blockers.

    Each insight includes:
    - Classification (type, priority, status)
    - Content (title, description, ai_reasoning)
    - Actions (JSONB array of suggested actions)
    - Metrics (evidence supporting the insight)
    - Source attribution (what triggered this insight)
    """

    __tablename__ = "deal_stakeholder_insights"
    __table_args__ = (
        # Primary query: All insights for a deal by status
        Index("ix_stakeholder_insight_deal_status", "deal_id", "status"),
        # Filter by stakeholder
        Index("ix_stakeholder_insight_stakeholder", "stakeholder_id", "status"),
        # Org-wide queries (dashboards)
        Index("ix_stakeholder_insight_org_status", "organisation_id", "status"),
        # Type-based filtering
        Index("ix_stakeholder_insight_type", "organisation_id", "insight_type"),
        # Priority-based filtering
        Index("ix_stakeholder_insight_priority", "organisation_id", "priority"),
    )

    # Identity
    insight_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Internal UUID for the insight record.",
    )

    # Context
    organisation_id: UUID = Field(
        index=True,
        description="Organisation this insight belongs to.",
    )
    deal_id: UUID = Field(
        foreign_key="crm_deals.deal_id",
        index=True,
        description="The deal this insight is associated with.",
    )
    stakeholder_id: UUID | None = Field(
        default=None,
        foreign_key="deal_stakeholders.stakeholder_id",
        index=True,
        description="Stakeholder this insight is about (null for deal-level insights).",
    )

    # Classification
    insight_type: StakeholderInsightType = Field(
        sa_column=Column(String(64), nullable=False),
        description="Type of insight (ENGAGEMENT_GAP, SENTIMENT_DECLINING, etc.).",
    )
    status: StakeholderInsightStatus = Field(
        default=StakeholderInsightStatus.ACTIVE,
        sa_column=Column(String(32), nullable=False, server_default=text("'ACTIVE'")),
        description="Current lifecycle status.",
    )
    priority: StakeholderInsightPriority = Field(
        default=StakeholderInsightPriority.MEDIUM,
        sa_column=Column(String(16), nullable=False, server_default=text("'MEDIUM'")),
        description="Priority level of the insight.",
    )

    # Content
    title: str = Field(
        sa_column=Column(String(500), nullable=False),
        description="Short title summarizing the insight.",
    )
    description: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Detailed explanation of the insight.",
    )

    # AI Metadata
    ai_reasoning: str = Field(
        sa_column=Column(Text, nullable=False),
        description="Explanation of WHY this insight was generated.",
    )
    ai_confidence: float = Field(
        default=0.8,
        sa_column=Column(Float, nullable=False, server_default=text("0.8")),
        ge=0.0,
        le=1.0,
        description="AI confidence score (0.0 - 1.0).",
    )

    # Actions
    # Format: [{"action_type": "SCHEDULE_CALL", "label": "Schedule Call", "parameters": {}}]
    actions: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_ARRAY_SQL)),
        description="List of suggested actions with action_type, label, and optional parameters.",
    )

    # Source Attribution
    source_type: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
        description="What triggered this insight (analysis_run, risk_blocker, etc.).",
    )
    source_record_id: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True),
        description="ID of the source record (analysis_run_id, risk_blocker_id, etc.).",
    )
    related_risk_blocker_id: UUID | None = Field(
        default=None,
        description="Link to DealRiskBlocker for UNADDRESSED_CONCERN type.",
    )

    # Stakeholder Context (denormalized for API efficiency)
    stakeholder_name: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Name of the related stakeholder (denormalized).",
    )
    stakeholder_title: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Job title of the related stakeholder (denormalized).",
    )
    stakeholder_email: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Email of the related stakeholder (denormalized).",
    )

    # Metrics (evidence for the insight)
    metric_value: float | None = Field(
        default=None,
        description="Numeric value supporting the insight (e.g., 21 days since contact).",
    )
    metric_unit: str | None = Field(
        default=None,
        sa_column=Column(String(32), nullable=True),
        description="Unit of the metric (e.g., 'days', 'percent').",
    )
    threshold_value: float | None = Field(
        default=None,
        description="Threshold that was exceeded (e.g., 14 days threshold).",
    )
    trend_direction: str | None = Field(
        default=None,
        sa_column=Column(String(16), nullable=True),
        description="Direction of change ('declining', 'improving', 'stable').",
    )

    # Timestamps
    identified_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="When this insight was first identified.",
    )
    expires_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="When this insight becomes stale/irrelevant.",
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )

    # Extended Data
    insight_data: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
        description="Additional data: previous_values, comparison_data, audit_trail.",
    )

    # Computed Properties
    @property
    def previous_values(self) -> dict[str, Any] | None:
        """Previous stakeholder values that triggered the insight."""
        return self.insight_data.get("previous_values")

    @property
    def current_values(self) -> dict[str, Any] | None:
        """Current stakeholder values at time of insight generation."""
        return self.insight_data.get("current_values")


__all__ = [
    # Enums
    "StakeholderInsightType",
    "StakeholderInsightStatus",
    "StakeholderInsightPriority",
    "InsightActionType",
    # Types
    "InsightActionDict",
    # Constants
    "INSIGHT_ACTION_LABELS",
    "DEFAULT_ACTIONS_BY_TYPE",
    "ENGAGEMENT_GAP_ACTIONS",
    "SENTIMENT_DECLINE_ACTIONS",
    "CHAMPION_OPPORTUNITY_ACTIONS",
    "UNADDRESSED_CONCERN_ACTIONS",
    "STAKEHOLDER_RISK_ACTIONS",
    "MISSING_DECISION_MAKER_ACTIONS",
    "COVERAGE_GAP_ACTIONS",
    # Functions
    "create_insight_action",
    "get_default_actions",
    # Model
    "DealStakeholderInsight",
]
