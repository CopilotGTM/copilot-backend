from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship
from src.domain.models.organisation_tables.sync.base import SyncedRecordMixin
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
    HOT = "HOT"
    WARM = "WARM"
    COLD = "COLD"


class StakeholderSource(str, Enum):
    CRM = "CRM"
    CALENDAR_DISCOVERY = "CALENDAR_DISCOVERY"
    MANUAL = "MANUAL"


class DealStakeholder(SyncedRecordMixin, table=True):
    __tablename__ = "deal_stakeholders"
    __table_args__ = (
        UniqueConstraint("deal_id", "email", name="uq_deal_stakeholder_email"),
        Index("ix_deal_stakeholder_org_deal", "organisation_id", "deal_id"),
        Index("ix_deal_stakeholder_email", "email"),
    )

    stakeholder_id: UUID = Field(default_factory=uuid4, primary_key=True)

    # Links
    deal_id: UUID = Field(foreign_key="crm_deals.deal_id", index=True)
    contact_id: UUID | None = Field(default=None, foreign_key="crm_contacts.contact_id", index=True)

    # Identity
    email: str = Field(sa_column=Column(String(255), nullable=False))
    name: str | None = Field(default=None)
    job_title: str | None = Field(default=None)
    department: str | None = Field(
        default=None, description="Department inferred from title or external sources"
    )

    # Flexible links (LinkedIn, CRM, Twitter, etc.)
    # Stored as a list of dicts: [{ "platform": "linkedin", "url": "...", "label": "...", "added_at": "..." }]
    external_links: list[dict] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
        description="List of external links with metadata",
    )

    # Insights
    stance: StakeholderStance = Field(default=StakeholderStance.UNKNOWN)
    decision_power: DecisionPower = Field(default=DecisionPower.UNKNOWN)
    champion_potential: float = Field(default=0.0, description="0-100 score")

    motivation: str | None = Field(default=None, sa_column=Column(String, nullable=True))
    pain_points: list[str] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    keywords: list[str] | None = Field(default=None, sa_column=Column(JSONB, nullable=True))

    # Engagement Metrics
    last_contact_at: datetime | None = Field(
        sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    engagement_score: float = Field(default=0.0)
    temperature: StakeholderTemperature = Field(default=StakeholderTemperature.COLD)

    source: StakeholderSource = Field(default=StakeholderSource.CALENDAR_DISCOVERY)

    # Relationships
    deal: CRMDeal = Relationship()
    contact: CRMContact | None = Relationship()
