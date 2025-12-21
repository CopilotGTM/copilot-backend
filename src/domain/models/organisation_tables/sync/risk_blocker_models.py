"""Deal risks and blockers models for AI-powered and manual tracking.

This module provides models for tracking deal risks and blockers that are either:
- AI-extracted from meeting transcripts, emails, Slack conversations
- Manually added by users

Key features:
- Provenance tracking (AI vs user created/modified)
- Full audit trail in metadata JSONB
- Source attribution to original content
- Lifecycle management (AI_SUGGESTED → OPEN → RESOLVED)
"""

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Boolean, Column, DateTime, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from ..base_mixins import utcnow

EMPTY_JSONB_SQL = "'{}'::jsonb"


class RiskBlockerType(str, Enum):
    """Classification of the item as risk or blocker."""

    RISK = "RISK"  # Potential issue that may impact deal
    BLOCKER = "BLOCKER"  # Active impediment that must be resolved


class RiskBlockerStatus(str, Enum):
    """Lifecycle status of the risk/blocker."""

    # AI Workflow States
    AI_SUGGESTED = "AI_SUGGESTED"  # Identified by AI, pending review
    AI_DISMISSED = "AI_DISMISSED"  # Dismissed by AI as low-confidence

    # User Workflow States
    OPEN = "OPEN"  # Confirmed as active issue
    IN_PROGRESS = "IN_PROGRESS"  # Being actively addressed
    RESOLVED = "RESOLVED"  # Issue has been resolved
    DISMISSED = "DISMISSED"  # User dismissed as not relevant
    WONT_FIX = "WONT_FIX"  # Acknowledged but not addressing


class RiskBlockerSeverity(str, Enum):
    """Severity/impact level of the risk or blocker."""

    LOW = "LOW"  # Minor impact, can proceed
    MEDIUM = "MEDIUM"  # Moderate impact, needs attention
    HIGH = "HIGH"  # Significant impact, priority attention
    CRITICAL = "CRITICAL"  # Deal-breaking, immediate action required


class RiskBlockerCategory(str, Enum):
    """Category/domain of the risk or blocker.

    Categories aligned with common pre-sales deal obstacles.
    """

    # Technical Categories
    TECHNICAL = "TECHNICAL"  # Technical requirements/compatibility
    INTEGRATION = "INTEGRATION"  # Integration challenges
    SECURITY = "SECURITY"  # Security/compliance concerns
    PERFORMANCE = "PERFORMANCE"  # Performance/scalability concerns

    # Commercial Categories
    BUDGET = "BUDGET"  # Budget/pricing concerns
    TIMELINE = "TIMELINE"  # Timeline/deadline concerns
    CONTRACT = "CONTRACT"  # Contract/legal terms

    # Stakeholder Categories
    CHAMPION_RISK = "CHAMPION_RISK"  # Champion leaving/changing roles
    STAKEHOLDER = "STAKEHOLDER"  # Stakeholder alignment issues
    COMPETITION = "COMPETITION"  # Competitive pressure

    # Process Categories
    INTERNAL_PROCESS = "INTERNAL_PROCESS"  # Customer internal process
    RESOURCE = "RESOURCE"  # Resource availability

    # General
    OTHER = "OTHER"


class RiskBlockerSource(str, Enum):
    """Source system where the risk/blocker was identified.

    Follows the same pattern as TimelineEventSource for consistency.
    Maps to the SOURCE column in the UI with platform-specific icons.
    """

    # Email sources
    EMAIL_GMAIL = "EMAIL_GMAIL"  # Gmail email thread
    EMAIL_OUTLOOK = "EMAIL_OUTLOOK"  # Outlook email thread

    # Meeting/Calendar sources
    MEETING_GOOGLE = "MEETING_GOOGLE"  # Google Calendar meeting
    MEETING_OUTLOOK = "MEETING_OUTLOOK"  # Outlook Calendar meeting
    TEAMS_MEETING = "TEAMS_MEETING"  # Microsoft Teams meeting
    ZOOM_MEETING = "ZOOM_MEETING"  # Zoom meeting

    # Messaging sources
    SLACK = "SLACK"  # Slack message/channel
    TEAMS_CHAT = "TEAMS_CHAT"  # Microsoft Teams chat

    # CRM sources
    CRM_HUBSPOT = "CRM_HUBSPOT"  # HubSpot CRM note/activity
    CRM_SALESFORCE = "CRM_SALESFORCE"  # Salesforce CRM note/activity

    # Manual sources
    MANUAL = "MANUAL"  # Manually added by user

    # System sources
    SYSTEM = "SYSTEM"  # System-detected (e.g., stale deal)
    OTHER = "OTHER"  # Other/unspecified source


class DealRiskBlocker(SQLModel, table=True):
    """
    AI-identified or manually tracked risks and blockers for deals.

    This table stores potential deal obstacles extracted from meeting transcripts,
    emails, Slack conversations, or manually added by users. Each item goes through
    a lifecycle from AI suggestion to resolution.

    Key design principles:
    1. Source attribution: Links to original source (transcript, email, etc.)
    2. Lifecycle tracking: Status workflow from suggestion to resolution
    3. AI confidence: Confidence score for AI-extracted items
    4. Provenance: Track AI vs user created, full audit trail in metadata
    """

    __tablename__ = "deal_risks_blockers"
    __table_args__ = (
        # Primary query: All items for a deal by status
        Index("ix_risk_blocker_deal_status", "deal_id", "status"),
        # Filter by type within a deal
        Index("ix_risk_blocker_deal_type", "deal_id", "item_type"),
        # Org-wide queries (dashboards, reports)
        Index("ix_risk_blocker_org_status", "organisation_id", "status"),
        Index("ix_risk_blocker_org_severity", "organisation_id", "severity"),
        # Category analysis
        Index("ix_risk_blocker_category", "organisation_id", "category"),
        # Source tracking
        Index("ix_risk_blocker_source_ref", "source_type", "source_record_id"),
        # AI-generated items pending review
        Index("ix_risk_blocker_ai_suggested", "organisation_id", "is_ai_generated", "status"),
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Identity
    # ─────────────────────────────────────────────────────────────────────────
    risk_blocker_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Internal UUID for the risk/blocker record.",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Context
    # ─────────────────────────────────────────────────────────────────────────
    organisation_id: UUID = Field(
        index=True,
        description="Organisation this risk/blocker belongs to.",
    )
    deal_id: UUID = Field(
        foreign_key="crm_deals.deal_id",
        index=True,
        description="The deal this risk/blocker is associated with.",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Classification
    # ─────────────────────────────────────────────────────────────────────────
    item_type: RiskBlockerType = Field(
        sa_column=Column(String(32), nullable=False),
        description="Whether this is a RISK or BLOCKER.",
    )
    category: RiskBlockerCategory = Field(
        default=RiskBlockerCategory.OTHER,
        sa_column=Column(String(32), nullable=False, server_default=text("'OTHER'")),
        description="Domain category of the risk/blocker.",
    )
    severity: RiskBlockerSeverity = Field(
        default=RiskBlockerSeverity.MEDIUM,
        sa_column=Column(String(32), nullable=False, server_default=text("'MEDIUM'")),
        description="Impact severity level.",
    )
    status: RiskBlockerStatus = Field(
        default=RiskBlockerStatus.AI_SUGGESTED,
        sa_column=Column(String(32), nullable=False, server_default=text("'AI_SUGGESTED'")),
        description="Current lifecycle status.",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Content
    # ─────────────────────────────────────────────────────────────────────────
    title: str = Field(
        sa_column=Column(String(500), nullable=False),
        description="Short title/summary of the risk or blocker.",
    )
    description: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Detailed description of the issue.",
    )
    quote: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Direct quote from source (transcript/email) if extracted by AI.",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Source Attribution
    # ─────────────────────────────────────────────────────────────────────────
    source_type: RiskBlockerSource = Field(
        default=RiskBlockerSource.MANUAL,
        sa_column=Column(String(32), nullable=False, server_default=text("'MANUAL'")),
        description="Source where this was identified.",
    )
    source_record_id: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True),
        description="ID of source record (meeting_id, email_association_id, etc.).",
    )
    source_context: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Human-readable source context (e.g., 'Discovery Call - Oct 7').",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # AI Metadata
    # ─────────────────────────────────────────────────────────────────────────
    ai_confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="AI confidence score (0.0 - 1.0) for extracted items.",
    )
    ai_reasoning: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="AI explanation for why this was identified as risk/blocker.",
    )
    extraction_run_id: UUID | None = Field(
        default=None,
        description="ID of the AI extraction job that created this item.",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Assignment
    # ─────────────────────────────────────────────────────────────────────────
    owner_name: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Name of person responsible for addressing this.",
    )
    owner_email: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Email of person responsible.",
    )
    assigned_team: str | None = Field(
        default=None,
        sa_column=Column(String(128), nullable=True),
        description="Team responsible (e.g., 'engineering', 'legal', 'finance').",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Resolution
    # ─────────────────────────────────────────────────────────────────────────
    resolution_notes: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Notes on how this was resolved.",
    )
    resolved_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="When this was marked as resolved.",
    )
    resolved_by: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Who resolved this issue.",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Provenance & Audit
    # ─────────────────────────────────────────────────────────────────────────
    is_ai_generated: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
        description="True if originally created by AI extraction, False if manually added.",
    )
    created_by_user_id: UUID | None = Field(
        default=None,
        description="User ID who created this item (null for AI-generated).",
    )
    created_by_user_name: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Display name of user who created this item.",
    )
    last_modified_by_user_id: UUID | None = Field(
        default=None,
        description="User ID who last modified this item.",
    )
    last_modified_by_user_name: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Display name of user who last modified this item.",
    )
    user_confirmed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="When a user first confirmed/accepted an AI-suggested item.",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Timestamps
    # ─────────────────────────────────────────────────────────────────────────
    identified_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="When this risk/blocker was first identified.",
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Extended Data (includes audit_trail)
    # ─────────────────────────────────────────────────────────────────────────
    risk_blocker_data: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
        description="Additional data including audit_trail, mentioned_stakeholders, etc.",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Computed Properties
    # ─────────────────────────────────────────────────────────────────────────
    @property
    def modification_count(self) -> int:
        """Total number of modifications (excluding creation)."""
        trail: list[dict[str, Any]] = self.risk_blocker_data.get("audit_trail", [])
        return len([e for e in trail if e.get("action") != "created"])

    @property
    def unique_modifier_count(self) -> int:
        """Number of unique users who modified this item."""
        modifiers: list[str] = self.risk_blocker_data.get("unique_modifiers", [])
        return len(modifiers)

    @property
    def audit_trail(self) -> list[dict[str, Any]]:
        """Full audit trail from risk_blocker_data."""
        trail: list[dict[str, Any]] = self.risk_blocker_data.get("audit_trail", [])
        return trail


__all__ = [
    "RiskBlockerType",
    "RiskBlockerStatus",
    "RiskBlockerSeverity",
    "RiskBlockerCategory",
    "RiskBlockerSource",
    "DealRiskBlocker",
]
