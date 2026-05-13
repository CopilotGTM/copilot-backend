from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy import Column, DateTime, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship
from src.domain.models.organisation_tables.sync.base import FlexibleSyncedRecordMixin
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal


class StakeholderStance(str, Enum):
    UNKNOWN = "UNKNOWN"
    CHAMPION = "CHAMPION"
    SUPPORTER = "SUPPORTER"
    NEUTRAL = "NEUTRAL"
    DETRACTOR = "DETRACTOR"
    BLOCKER = "BLOCKER"


class DecisionPower(str, Enum):
    UNKNOWN = "UNKNOWN"
    DECISION_MAKER = "DECISION_MAKER"
    INFLUENCER = "INFLUENCER"
    EVALUATOR = "EVALUATOR"
    GATEKEEPER = "GATEKEEPER"
    END_USER = "END_USER"


class StakeholderTemperature(str, Enum):
    """Temperature indicating engagement level with stakeholder.

    TODO: Existing records may have COLD as old default. Future engagement
    analysis tasks should properly assess and update temperatures. Do not
    bulk-update existing COLD records to UNKNOWN - let analysis determine
    the correct value based on actual engagement data.
    """

    UNKNOWN = "UNKNOWN"
    HOT = "HOT"
    WARM = "WARM"
    COLD = "COLD"


class StakeholderSource(str, Enum):
    CRM = "CRM"
    CALENDAR_DISCOVERY = "CALENDAR_DISCOVERY"
    EMAIL_DISCOVERY = "EMAIL_DISCOVERY"
    MANUAL = "MANUAL"


class ApprovalStatus(str, Enum):
    """Approval status for stakeholders.

    - CRM stakeholders are always APPROVED (trusted source)
    - CALENDAR_DISCOVERY: First 5 per deal auto-APPROVED, rest PENDING
    - EMAIL_DISCOVERY: First 5 per deal auto-APPROVED, rest PENDING
    - User can REJECT any stakeholder to exclude from matching
    - User can REMOVE an APPROVED stakeholder from the deal
    """

    APPROVED = "APPROVED"
    PENDING = "PENDING"
    REJECTED = "REJECTED"
    REMOVED = "REMOVED"


class DealStakeholder(FlexibleSyncedRecordMixin, table=True):
    __tablename__ = "deal_stakeholders"
    __table_args__ = (
        UniqueConstraint("deal_id", "email", name="uq_deal_stakeholder_email"),
        Index("ix_deal_stakeholder_org_deal", "organisation_id", "deal_id"),
        Index("ix_deal_stakeholder_email", "email"),
        Index(
            "ix_deal_stakeholder_org_deal_email_active",
            "organisation_id",
            "deal_id",
            "email",
            "is_deleted",
        ),
        Index(
            "ix_deal_stakeholder_org_deal_approval_active",
            "organisation_id",
            "deal_id",
            "approval_status",
            "is_deleted",
        ),
    )

    stakeholder_id: UUID = Field(default_factory=uuid4, primary_key=True)

    # Links
    deal_id: UUID = Field(foreign_key="crm_deals.deal_id", index=True)
    contact_id: UUID | None = Field(default=None, foreign_key="crm_contacts.contact_id", index=True)
    created_by_user_id: UUID | None = Field(
        default=None,
        index=True,
        description="User who added this stakeholder. NULL for legacy records. AUTOMATED_PIPELINE_USER_ID for automated sources.",
    )

    # Identity
    email: str = Field(sa_column=Column(String(255), nullable=False))
    name: str | None = Field(default=None)
    job_title: str | None = Field(default=None)
    department: str | None = Field(
        default=None, description="Department inferred from title or external sources"
    )
    phone_number: str | None = Field(
        default=None,
        sa_column=Column(String(50), nullable=True),
        description="Phone number as provided by CRM or user. Stored as-is, no formatting applied.",
    )

    # Flexible links (LinkedIn, CRM, Twitter, etc.)
    # Stored as a list of dicts: [{ "platform": "linkedin", "url": "...", "label": "...", "added_at": "..." }]
    external_links: list[dict] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
        description="List of external links with metadata",
    )

    # Insights (enum columns stored as VARCHAR for flexible schema evolution)
    stance: StakeholderStance = Field(
        default=StakeholderStance.UNKNOWN,
        sa_column=Column(String(64), nullable=False, server_default=text("'UNKNOWN'")),
    )
    decision_power: DecisionPower = Field(
        default=DecisionPower.UNKNOWN,
        sa_column=Column(String(64), nullable=False, server_default=text("'UNKNOWN'")),
    )
    champion_potential: float = Field(default=0.0, description="0-100 score")

    motivation: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    pain_points: list[str] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    keywords: list[str] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))

    # Engagement Metrics
    last_contact_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    engagement_score: float = Field(default=0.0)
    temperature: StakeholderTemperature = Field(
        default=StakeholderTemperature.UNKNOWN,
        sa_column=Column(String(64), nullable=False, server_default=text("'UNKNOWN'")),
    )

    source: StakeholderSource = Field(
        default=StakeholderSource.CALENDAR_DISCOVERY,
        sa_column=Column(String(64), nullable=False, server_default=text("'CALENDAR_DISCOVERY'")),
    )
    source_reference_id: UUID | None = Field(
        default=None,
        index=True,
        description="Reference to the source record (e.g., CRMDealParticipant.participant_id when source=CRM).",
    )
    approval_status: ApprovalStatus = Field(
        default=ApprovalStatus.APPROVED,
        sa_column=Column(String(64), nullable=False, index=True, server_default=text("'APPROVED'")),
        description="Approval status. CRM=always APPROVED. Discovery=auto-APPROVED up to 5 per source per deal.",
    )

    # Extended Data (general purpose, future extensibility)
    stakeholder_data: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True, server_default=text("NULL")),
        description="General extended data for future extensibility.",
    )

    # Engagement History (time-series tracking for insight detection)
    # Structure: [
    #   {
    #     "recorded_at": "2025-01-06T10:00:00Z",
    #     "stance": "CHAMPION",
    #     "temperature": "HOT",
    #     "engagement_score": 85.0,
    #     "champion_potential": 90.0,
    #     "decision_power": "DECISION_MAKER",
    #     "analysis_run_id": "uuid"
    #   },
    #   ...
    # ]
    stakeholder_engagement_history: list[dict[str, Any]] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True, server_default=text("NULL")),
        description="Time-series engagement history for tracking changes and detecting sentiment trends.",
    )

    # LinkedIn / Apollo research (markdown profile generated by StakeholderResearchAgent)
    stakeholder_research: str | None = Field(
        default=None,
        sa_column=Column(sa.Text, nullable=True, server_default=text("NULL")),
        description="Markdown research profile from StakeholderResearchAgent.",
    )
    last_researched_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )

    # Relationships
    deal: CRMDeal = Relationship()
    contact: CRMContact | None = Relationship()
