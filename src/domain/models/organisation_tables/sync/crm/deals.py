from datetime import datetime
from enum import Enum
from typing import Any
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
    __table_args__ = (
        UniqueConstraint("integration_id", "external_id", name="uq_crm_pipeline_external"),
        Index("ix_crm_pipeline_org_name", "organisation_id", "name"),
    )

    pipeline_id: UUID = Field(default_factory=uuid4, primary_key=True)
    external_id: str = Field(sa_column=Column(String(255), nullable=False))
    data_payload: dict | None = Field(default=None, sa_column=Column(JSONB))
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
        UniqueConstraint("integration_id", "external_id", name="uq_crm_stage_external"),
        Index("ix_crm_stage_pipeline_order", "pipeline_id", "display_order"),
        Index("ix_crm_stage_org_name", "organisation_id", "name"),
        CheckConstraint(
            "(probability IS NULL) OR (probability >= 0 AND probability <= 100)",
            name="ck_crm_stage_probability",
        ),
    )

    stage_id: UUID = Field(default_factory=uuid4, primary_key=True)
    external_id: str = Field(sa_column=Column(String(255), nullable=False))
    data_payload: dict | None = Field(default=None, sa_column=Column(JSONB))
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
    pipeline_id: UUID = Field(foreign_key="crm_pipelines.pipeline_id", index=True)
    name: str = Field(sa_column=Column(String(255), nullable=False, index=True))
    label: str | None = Field(default=None)
    stage_key: str | None = Field(
        default=None,
        sa_column=Column(String(128), nullable=True, index=True),
        description="Provider stage identifier (e.g., probability bucket key)",
    )
    probability: float | None = Field(default=None)
    stage_state: CRMStageState = Field(
        default=CRMStageState.OPEN,
        sa_column=Column(String(16), nullable=False, server_default=text("'open'")),
    )
    forecast_category: str | None = Field(default=None, sa_column=Column(String(64)))
    display_order: int | None = Field(default=None)
    is_closed: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )

    pipeline: CRMPipeline | None = Relationship(back_populates="stages")


class CRMDeal(SyncedRecordMixin, table=True):
    """Canonical deal record across CRMs."""

    __tablename__ = "crm_deals"
    __table_args__ = (
        UniqueConstraint("integration_id", "external_id", name="uq_crm_deal_external"),
        Index("ix_crm_deal_org_stage", "organisation_id", "stage_id"),
        Index("ix_crm_deal_org_pipeline", "organisation_id", "pipeline_id"),
        Index("ix_crm_deal_pipeline_external", "pipeline_external_id"),
        Index("ix_crm_deal_stage_external", "stage_external_id"),
        Index("ix_crm_deal_owner", "owner_external_id"),
        Index("ix_crm_deal_status", "organisation_id", "deal_status"),
        Index("ix_crm_deal_close_date", "close_date"),
        CheckConstraint(
            "(probability IS NULL) OR (probability >= 0 AND probability <= 100)",
            name="ck_crm_deal_probability",
        ),
    )

    deal_id: UUID = Field(default_factory=uuid4, primary_key=True)
    external_id: str = Field(sa_column=Column(String(255), nullable=False))
    data_payload: dict | None = Field(default=None, sa_column=Column(JSONB))
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
    pipeline_id: UUID | None = Field(
        default=None,
        foreign_key="crm_pipelines.pipeline_id",
        index=True,
    )
    stage_id: UUID | None = Field(
        default=None,
        foreign_key="crm_deal_stages.stage_id",
        index=True,
    )
    pipeline_external_id: str | None = Field(default=None, index=True)
    pipeline_name: str | None = Field(default=None)
    pipeline_label: str | None = Field(default=None)
    stage_external_id: str | None = Field(default=None, index=True)
    stage_name: str | None = Field(default=None)
    stage_label: str | None = Field(default=None)

    name: str | None = Field(default=None, sa_column=Column(String(500)))
    description: str | None = Field(default=None)

    amount: float | None = Field(
        default=None,
        sa_column=Column(Numeric(18, 2), nullable=True),
    )
    amount_home_currency: float | None = Field(
        default=None,
        sa_column=Column(Numeric(18, 2), nullable=True),
    )
    currency: str | None = Field(
        default=None,
        sa_column=Column(String(3), nullable=True),
    )
    probability: float | None = Field(default=None)
    predicted_amount: float | None = Field(
        default=None,
        sa_column=Column(Numeric(18, 2), nullable=True),
        description="Forecasted amount (probability-weighted)",
    )
    deal_status: CRMDealStatus = Field(
        default=CRMDealStatus.OPEN,
        sa_column=Column(String(16), nullable=False, server_default=text("'open'")),
    )
    forecast_category: str | None = Field(default=None, sa_column=Column(String(64)))
    lead_source: str | None = Field(default=None, sa_column=Column(String(128)))
    deal_type: str | None = Field(default=None, sa_column=Column(String(128)))
    priority: str | None = Field(default=None, sa_column=Column(String(64)))

    owner_external_id: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True, index=True),
    )
    owner_email: str | None = Field(default=None, sa_column=Column(String(255)))
    owner_name: str | None = Field(default=None, sa_column=Column(String(255)))

    company_external_id: str | None = Field(default=None, sa_column=Column(String(255)))
    primary_contact_external_id: str | None = Field(default=None, sa_column=Column(String(255)))

    close_date: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    closed_won_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    closed_lost_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    closed_reason: str | None = Field(default=None, sa_column=Column(String(255)))

    expected_close_date: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    last_activity_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    stage_entered_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    stage_updated_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    source_created_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )

    properties: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(
            JSONB,
            nullable=False,
            server_default=text(EMPTY_JSONB_SQL),
        ),
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
    associations_payload: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
        description="Raw associations block from provider",
    )

    participants: list["CRMDealParticipant"] = Relationship(back_populates="deal")
    stage_history: list["CRMDealStageHistory"] = Relationship(back_populates="deal")


class CRMDealParticipant(SyncedRecordMixin, table=True):
    """Links deals to contacts, companies, owners, or other CRM entities."""

    __tablename__ = "crm_deal_participants"
    __table_args__ = (
        UniqueConstraint("integration_id", "external_id", name="uq_crm_deal_participant_external"),
        UniqueConstraint(
            "deal_id",
            "participant_type",
            "related_external_id",
            "relationship_role",
            name="uq_crm_deal_participant_unique",
        ),
        Index("ix_crm_deal_participant_type", "participant_type"),
        Index("ix_crm_deal_participant_related", "related_external_id"),
        Index("ix_crm_deal_participant_org", "organisation_id", "participant_type"),
    )

    participant_id: UUID = Field(default_factory=uuid4, primary_key=True)
    external_id: str = Field(sa_column=Column(String(255), nullable=False))
    data_payload: dict | None = Field(default=None, sa_column=Column(JSONB))
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
    deal_id: UUID = Field(foreign_key="crm_deals.deal_id", index=True)

    participant_type: CRMParticipantType = Field(
        sa_column=Column(String(32), nullable=False),
    )
    relationship_role: str | None = Field(
        default=None,
        sa_column=Column(String(128), nullable=True, index=True),
        description="Role within the deal (e.g., primary_contact, decision_maker)",
    )

    related_external_id: str = Field(sa_column=Column(String(255), nullable=False))
    related_local_id: UUID | None = Field(default=None, index=True)
    related_name: str | None = Field(default=None, sa_column=Column(String(255)))
    related_email: str | None = Field(default=None, sa_column=Column(String(255)))

    deal: CRMDeal | None = Relationship(back_populates="participants")


class CRMDealStageHistory(SyncedRecordMixin, table=True):
    """Stage transition history for deals."""

    __tablename__ = "crm_deal_stage_history"
    __table_args__ = (
        UniqueConstraint(
            "integration_id", "external_id", name="uq_crm_deal_stage_history_external"
        ),
        Index("ix_crm_stage_history_deal", "deal_id", "entered_at"),
    )

    history_id: UUID = Field(default_factory=uuid4, primary_key=True)
    external_id: str = Field(sa_column=Column(String(255), nullable=False))
    data_payload: dict | None = Field(default=None, sa_column=Column(JSONB))
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
    deal_id: UUID = Field(foreign_key="crm_deals.deal_id", index=True)
    stage_id: UUID | None = Field(
        default=None,
        foreign_key="crm_deal_stages.stage_id",
        index=True,
    )
    stage_external_id: str | None = Field(default=None, sa_column=Column(String(255)))
    from_stage_external_id: str | None = Field(default=None, sa_column=Column(String(255)))

    entered_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="Timestamp the deal entered this stage",
    )
    exited_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    elapsed_seconds: int | None = Field(default=None)

    amount_at_stage: float | None = Field(
        default=None,
        sa_column=Column(Numeric(18, 2), nullable=True),
    )
    probability_at_stage: float | None = Field(default=None)
    source: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
        description="Origin of the change (api, workflow, user) when provided",
    )

    deal: CRMDeal | None = Relationship(back_populates="stage_history")
