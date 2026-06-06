from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship

from ...base_mixins import utcnow
from ..base import SyncedRecordMixin
from ..integration_models import EMPTY_JSONB_SQL


class CRMParticipantType(str, Enum):
    """Types of CRM entities that can be tied to a deal."""

    CONTACT = "contact"
    COMPANY = "company"
    USER = "user"
    TICKET = "ticket"
    LINE_ITEM = "line_item"
    CUSTOM = "custom"


class CRMDealStatus(str, Enum):
    """High-level lifecycle state for a deal."""

    OPEN = "open"
    WON = "won"
    LOST = "lost"
    CLOSED = "closed"


class CRMStageState(str, Enum):
    """Categorisation of a stage for reporting."""

    OPEN = "open"
    WON = "won"
    LOST = "lost"


class CRMPipeline(SyncedRecordMixin, table=True):
    """CRM pipeline metadata (e.g., HubSpot deal pipeline)."""

    __tablename__ = "crm_pipelines"
    __table_args__ = (Index("ix_crm_pipeline_org_name", "organisation_id", "name"),)

    pipeline_id: UUID = Field(default_factory=uuid4, primary_key=True)
    synced_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    source_modified_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    last_seen_at: datetime | None = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    name: str = Field(sa_column=Column(String(255), nullable=False, index=True))
    label: str | None = Field(default=None)
    object_type: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True, index=True),
        description="Provider object type (e.g., deals, tickets)",
    )
    is_default: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    is_active: bool = Field(
        default=True,
        sa_column=Column(Boolean, nullable=False, server_default=text("true")),
    )
    display_order: int | None = Field(default=None)
    properties: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(
            JSONB,
            nullable=False,
            server_default=text(EMPTY_JSONB_SQL),
        ),
    )

    stages: list["CRMDealStage"] = Relationship(back_populates="pipeline")


class CRMDealStage(SyncedRecordMixin, table=True):
    """Stages within a CRM pipeline."""

    __tablename__ = "crm_deal_stages"
    __table_args__ = (
        Index("ix_crm_stage_pipeline_order", "pipeline_id", "display_order"),
        Index("ix_crm_stage_org_name", "organisation_id", "name"),
        Index("ix_crm_deal_stage_pipeline_active", "pipeline_id", "is_active"),
        CheckConstraint(
            "(probability IS NULL) OR (probability >= 0 AND probability <= 100)",
            name="ck_crm_stage_probability",
        ),
    )

    # Identity
    stage_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Internal UUID for the pipeline stage.",
    )
    # Labels
    name: str = Field(
        sa_column=Column(String(255), nullable=False, index=True),
        description="Stage name as displayed in the provider UI.",
    )
    label: str | None = Field(default=None, description="Human-friendly stage label.")
    stage_key: str | None = Field(
        default=None,
        sa_column=Column(String(128), nullable=True, index=True),
        description="Provider stage key or API identifier distinct from display name.",
    )

    # State
    stage_state: CRMStageState = Field(
        default=CRMStageState.OPEN,
        sa_column=Column(String(16), nullable=False, server_default=text("'open'")),
        description="Normalised state for reporting (open/won/lost).",
    )
    probability: float | None = Field(
        default=None,
        description="Default win probability percentage (0-100) for this stage.",
    )
    forecast_category: str | None = Field(
        default=None,
        sa_column=Column(String(64)),
        description="Provider forecast category for this stage.",
    )
    is_closed: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
        description="Whether the provider marks this stage as closed.",
    )
    is_active: bool = Field(
        default=True,
        sa_column=Column(Boolean, nullable=False, server_default=text("true")),
        description=(
            "False once the provider stops returning this stage in a snapshot. "
            "Historical deals keep referencing it; no hard/soft delete."
        ),
    )
    display_order: int | None = Field(
        default=None,
        description="Numeric order for stage display within its pipeline.",
    )

    # Pipeline linkage
    pipeline_id: UUID = Field(
        foreign_key="crm_pipelines.pipeline_id",
        index=True,
        description="Owning pipeline ID (FK).",
    )

    # Sync meta
    synced_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="Timestamp when this stage row was last synced.",
    )
    source_modified_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Last modified timestamp as reported by provider.",
    )
    last_seen_at: datetime | None = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Most recent time this stage was observed during sync.",
    )

    pipeline: CRMPipeline | None = Relationship(back_populates="stages")


class CRMDeal(SyncedRecordMixin, table=True):
    """Canonical deal record across CRMs."""

    __tablename__ = "crm_deals"
    __table_args__ = (
        Index("ix_crm_deal_org_stage", "organisation_id", "stage_id"),
        Index("ix_crm_deal_org_pipeline", "organisation_id", "pipeline_id"),
        Index("ix_crm_deal_created_by", "organisation_id", "created_by_user_id"),
        Index("ix_crm_deal_updated_by", "organisation_id", "updated_by_user_id"),
        Index("ix_crm_deal_company_id", "organisation_id", "company_id"),
        Index("ix_crm_deal_primary_contact_id", "organisation_id", "primary_contact_id"),
        Index("ix_crm_deal_status", "organisation_id", "deal_status"),
        Index("ix_crm_deal_close_date", "close_date"),
        Index(
            "ix_crm_deal_org_active_created",
            "organisation_id",
            "is_deleted",
            "created_at",
            "deal_id",
        ),
        Index(
            "ix_crm_deal_org_active_status_created",
            "organisation_id",
            "is_deleted",
            "deal_status",
            "created_at",
            "deal_id",
        ),
        Index(
            "ix_crm_deal_org_active_owner_created",
            "organisation_id",
            "is_deleted",
            "owner_id",
            "created_at",
            "deal_id",
        ),
        CheckConstraint(
            "(probability IS NULL) OR (probability >= 0 AND probability <= 100)",
            name="ck_crm_deal_probability",
        ),
    )
    # Identity
    deal_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Internal UUID for the normalised deal record.",
    )
    # Core business context
    name: str | None = Field(
        default=None,
        sa_column=Column(String(500)),
        description="Deal name or title as shown in the provider UI.",
    )
    amount: float | None = Field(
        default=None,
        sa_column=Column(Numeric(18, 2), nullable=True),
        description="Deal amount in the provider currency (unconverted).",
    )
    currency: str | None = Field(
        default=None,
        sa_column=Column(String(3), nullable=True),
        description="ISO 4217 currency code for the deal amount.",
    )
    amount_home_currency: float | None = Field(
        default=None,
        sa_column=Column(Numeric(18, 2), nullable=True),
        description="Deal amount converted to the organisation home currency, if available.",
    )
    deal_status: CRMDealStatus = Field(
        default=CRMDealStatus.OPEN,
        sa_column=Column(String(16), nullable=False, server_default=text("'open'")),
        description="Normalised lifecycle status derived from provider signals.",
    )
    probability: float | None = Field(
        default=None,
        description="Provider-supplied win probability percentage (0-100).",
    )
    predicted_amount: float | None = Field(
        default=None,
        sa_column=Column(Numeric(18, 2), nullable=True),
        description="Forecasted amount (probability-weighted)",
    )
    close_date: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Scheduled or actual close date for the deal.",
    )
    expected_close_date: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Provider-forecasted close date when the deal is expected to land.",
    )
    closed_won_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Timestamp when the deal was marked as won.",
    )
    closed_lost_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Timestamp when the deal was marked as lost.",
    )
    closed_reason: str | None = Field(
        default=None,
        sa_column=Column(String(255)),
        description="Provider-supplied reason for closing the deal (won or lost).",
    )
    crm_url: str | None = Field(
        default=None,
        sa_column=Column(String(500)),
        description="Direct URL to the deal in the CRM provider portal.",
    )
    # Sales process snapshot
    stage_name: str | None = Field(
        default=None,
        description="Provider stage name captured at sync time.",
    )
    stage_label: str | None = Field(
        default=None,
        description="Human-friendly stage label from the provider.",
    )
    pipeline_name: str | None = Field(
        default=None,
        description="Provider pipeline name captured at sync time.",
    )
    pipeline_label: str | None = Field(
        default=None,
        description="Human-friendly pipeline label from the provider.",
    )
    pipeline_id: UUID | None = Field(
        default=None,
        foreign_key="crm_pipelines.pipeline_id",
        index=True,
        description="Reference to the normalised pipeline row for this deal.",
    )
    stage_id: UUID | None = Field(
        default=None,
        foreign_key="crm_deal_stages.stage_id",
        index=True,
        description="Reference to the normalised current stage row for this deal.",
    )
    forecast_category: str | None = Field(
        default=None,
        sa_column=Column(String(64)),
        description="Provider forecast category classification (e.g., pipeline stages).",
    )
    deal_type: str | None = Field(
        default=None,
        sa_column=Column(String(128)),
        description="Provider-defined deal type or segment.",
    )
    priority: str | None = Field(
        default=None,
        sa_column=Column(String(64)),
        description="Provider-defined prioritisation or SLA tier.",
    )
    lead_source: str | None = Field(
        default=None,
        sa_column=Column(String(128)),
        description="Marketing or sales source attribution for the deal.",
    )

    # Ownership & associations
    owner_name: str | None = Field(
        default=None,
        sa_column=Column(String(255)),
        description="Display name for the deal owner.",
    )
    owner_email: str | None = Field(
        default=None,
        sa_column=Column(String(255)),
        description="Email address for the deal owner, if supplied.",
    )
    owner_id: UUID | None = Field(
        default=None,
        foreign_key="crm_users.user_id",
        description="Resolved internal user ID for the deal owner (FK to crm_users).",
    )
    created_by_user_id: UUID | None = Field(
        default=None,
        foreign_key="crm_users.user_id",
        description="Canonical internal CRM user ID for the actor that created this deal when resolved.",
    )
    updated_by_user_id: UUID | None = Field(
        default=None,
        foreign_key="crm_users.user_id",
        description="Canonical internal CRM user ID for the actor that last updated this deal when resolved.",
    )
    company_id: UUID | None = Field(
        default=None,
        foreign_key="crm_companies.company_id",
        description="Resolved internal company ID for the primary associated company.",
    )
    company_name: str | None = Field(
        default=None,
        sa_column=Column(String(255)),
        description="Display name of the associated company, if available.",
    )
    company_domain: str | None = Field(
        default=None,
        sa_column=Column(String(255)),
        description="Domain/website of the associated company, if available.",
    )
    primary_contact_id: UUID | None = Field(
        default=None,
        foreign_key="crm_contacts.contact_id",
        description="Resolved internal contact ID for the primary contact linked to the deal.",
    )
    primary_contact_name: str | None = Field(
        default=None,
        sa_column=Column(String(255)),
        description="Display name of the primary contact linked to the deal.",
    )
    primary_contact_email: str | None = Field(
        default=None,
        sa_column=Column(String(255)),
        description="Email address of the primary contact linked to the deal.",
    )

    # Activity & timing
    source_created_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Original creation timestamp recorded in the provider.",
    )
    last_activity_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Most recent activity timestamp associated with the deal.",
    )
    days_to_close: int | None = Field(
        default=None,
        description="HubSpot-computed days between creation and close dates.",
    )
    stage_entered_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Timestamp when the deal entered the current stage.",
    )
    stage_updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Timestamp when the current stage details were last refreshed.",
    )
    synced_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="Timestamp when this deal row was last synced into our system.",
    )
    source_modified_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Last modified timestamp reported by the provider.",
    )
    last_seen_at: datetime | None = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Most recent time this record was observed during sync.",
    )

    # Freeform details
    description: str | None = Field(
        default=None,
        description="Long-form description or notes stored with the deal.",
    )

    # Payloads & analytics
    properties: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(
            JSONB,
            nullable=False,
            server_default=text(EMPTY_JSONB_SQL),
        ),
        description="Flattened provider-specific deal properties we want indexed in JSON.",
    )
    metrics: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(
            JSONB,
            nullable=False,
            server_default=text(EMPTY_JSONB_SQL),
        ),
        description="Cached analytics metrics (e.g., ARR, ACV segments)",
    )
    associated_contact_count: int | None = Field(
        default=None,
        description="Number of contacts associated with the deal reported by the provider.",
    )
    associated_company_count: int | None = Field(
        default=None,
        description="Number of companies associated with the deal reported by the provider.",
    )
    note_count: int | None = Field(
        default=None,
        description="Number of notes logged on the deal as reported by the provider.",
    )
    # AI-generated content (not synced from CRM)
    deal_summary_data: dict | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
        description="AI-generated deal summary data with schema_version, generated_at, and summary content.",
    )

    participants: list["CRMDealParticipant"] = Relationship(back_populates="deal")
    stage_history: list["CRMDealStageHistory"] = Relationship(back_populates="deal")

    stage: Optional["CRMDealStage"] = Relationship()
    pipeline: Optional["CRMPipeline"] = Relationship()


class CRMDealParticipant(SyncedRecordMixin, table=True):
    """Links deals to contacts, companies, owners, or other CRM entities."""

    __tablename__ = "crm_deal_participants"
    __table_args__ = (
        UniqueConstraint(
            "deal_id",
            "participant_type",
            "related_local_id",
            "relationship_role",
            name="uq_crm_deal_participant_unique",
        ),
        Index("ix_crm_deal_participant_type", "participant_type"),
        Index("ix_crm_deal_participant_related", "related_local_id"),
        Index("ix_crm_deal_participant_org", "organisation_id", "participant_type"),
        Index(
            "ix_crm_deal_participant_org_deal_type_active",
            "organisation_id",
            "deal_id",
            "participant_type",
            "is_deleted",
        ),
    )

    # Identity
    participant_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Internal UUID for the deal participant record.",
    )
    # Linkage
    deal_id: UUID = Field(
        foreign_key="crm_deals.deal_id",
        index=True,
        description="Deal ID (FK) this participant is associated with.",
    )
    participant_type: CRMParticipantType = Field(
        sa_column=Column(String(32), nullable=False),
        description="Type of related CRM entity (contact/company/user/etc).",
    )
    relationship_role: str | None = Field(
        default=None,
        sa_column=Column(String(128), nullable=True, index=True),
        description="Role within the deal (e.g., primary_contact, decision_maker)",
    )

    # Related entity identifiers
    related_local_id: UUID | None = Field(
        default=None,
        index=True,
        description="Internal UUID of the related entity when resolved.",
    )
    related_name: str | None = Field(
        default=None,
        sa_column=Column(String(255)),
        description="Display name of the related entity, if known.",
    )
    related_email: str | None = Field(
        default=None,
        sa_column=Column(String(255)),
        description="Email of the related entity, if applicable.",
    )

    # Sync meta
    synced_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="Timestamp when this participant row was last synced.",
    )
    source_modified_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Last modified timestamp from provider.",
    )
    last_seen_at: datetime | None = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Most recent time this link was observed during sync.",
    )

    deal: CRMDeal | None = Relationship(back_populates="participants")


class CRMDealStageHistory(SyncedRecordMixin, table=True):
    """Stage transition history for deals."""

    __tablename__ = "crm_deal_stage_history"
    __table_args__ = (Index("ix_crm_stage_history_deal", "deal_id", "entered_at"),)

    # Identity
    history_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Internal UUID for the stage transition record.",
    )
    # Deal and stage linkage
    deal_id: UUID = Field(
        foreign_key="crm_deals.deal_id",
        index=True,
        description="Deal ID (FK) for which the transition occurred.",
    )
    stage_id: UUID | None = Field(
        default=None,
        foreign_key="crm_deal_stages.stage_id",
        index=True,
        description="Internal stage ID (FK) after the transition, when resolved.",
    )
    from_stage_id: UUID | None = Field(
        default=None,
        foreign_key="crm_deal_stages.stage_id",
        index=True,
        description="Internal stage ID (FK) the deal exited from, when resolved.",
    )
    # Transition timing
    entered_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="Timestamp the deal entered this stage",
    )
    exited_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Timestamp the deal exited this stage, if known.",
    )
    elapsed_seconds: int | None = Field(
        default=None,
        description="Computed seconds spent in the stage (if available).",
    )

    # Snapshot metrics
    amount_at_stage: float | None = Field(
        default=None,
        sa_column=Column(Numeric(18, 2), nullable=True),
        description="Deal amount snapshot taken at the time of entry.",
    )
    probability_at_stage: float | None = Field(
        default=None,
        description="Win probability snapshot (0-100) at the time of entry.",
    )

    # Origin
    source: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
        description="Origin of the change (api, workflow, user) when provided",
    )

    # Sync meta
    synced_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="Timestamp when this history row was last synced.",
    )
    source_modified_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Last modified timestamp reported by the provider.",
    )
    last_seen_at: datetime | None = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Most recent time this transition was observed during sync.",
    )

    deal: CRMDeal | None = Relationship(back_populates="stage_history")
