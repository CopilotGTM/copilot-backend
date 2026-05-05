"""CRM lead models for leads, participants, and status history."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Column, DateTime, Index, Numeric, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from ...base_mixins import utcnow
from ..base import FlexibleSyncedRecordMixin
from ..integration_models import EMPTY_JSONB_SQL


class CRMLeadSourceType(str, Enum):
    """Origin channel that created or imported the lead."""

    FORM = "form"
    MANUAL = "manual"
    IMPORT = "import"
    API = "api"
    PROVIDER_SYNC = "provider_sync"
    CHAT = "chat"
    WORKFLOW = "workflow"


class CRMLeadStatus(str, Enum):
    """Lead lifecycle before it becomes an opportunity."""

    NEW = "new"
    WORKING = "working"
    QUALIFIED = "qualified"
    UNQUALIFIED = "unqualified"
    CONVERTED = "converted"
    ARCHIVED = "archived"


class CRMLeadPriority(str, Enum):
    """Simple prioritisation for lead queues."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class CRMLeadParticipantType(str, Enum):
    """CRM object types that can participate in a lead before conversion."""

    CONTACT = "contact"
    COMPANY = "company"
    USER = "user"
    CUSTOM = "custom"


class CRMLead(FlexibleSyncedRecordMixin, table=True):
    """Pre-opportunity CRM record captured from forms, imports, APIs, or providers."""

    __tablename__ = "crm_leads"
    __table_args__ = (
        Index(
            "uq_crm_lead_provider_external",
            "integration_id",
            "external_id",
            unique=True,
            postgresql_where=text("integration_id IS NOT NULL AND external_id IS NOT NULL"),
        ),
        Index("ix_crm_lead_org_status_captured", "organisation_id", "status", "captured_at"),
        Index("ix_crm_lead_org_owner_status", "organisation_id", "owner_id", "status"),
        Index(
            "ix_crm_lead_org_email_active",
            "organisation_id",
            "email",
            postgresql_where=text("email IS NOT NULL AND is_deleted = false"),
        ),
        Index(
            "ix_crm_lead_org_company_domain_active",
            "organisation_id",
            "company_domain",
            postgresql_where=text("company_domain IS NOT NULL AND is_deleted = false"),
        ),
        Index(
            "ix_crm_lead_org_form_captured",
            "organisation_id",
            "form_id",
            "captured_at",
            postgresql_where=text("form_id IS NOT NULL"),
        ),
        Index(
            "ix_crm_lead_org_converted_deal",
            "organisation_id",
            "converted_deal_id",
            postgresql_where=text("converted_deal_id IS NOT NULL"),
        ),
        CheckConstraint(
            "status != 'converted' OR (converted_deal_id IS NOT NULL AND converted_at IS NOT NULL)",
            name="ck_crm_lead_converted_requires_deal",
        ),
        CheckConstraint(
            "budget_amount IS NULL OR budget_amount >= 0",
            name="ck_crm_lead_budget_non_negative",
        ),
        CheckConstraint("score IS NULL OR score >= 0", name="ck_crm_lead_score_non_negative"),
    )

    lead_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Internal UUID for the lead.",
    )
    external_id: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Provider lead ID when synced from an external CRM.",
    )

    source_type: CRMLeadSourceType = Field(
        default=CRMLeadSourceType.MANUAL,
        sa_column=Column(String(32), nullable=False, server_default=text("'manual'")),
        description="How the lead entered the system.",
    )
    source_channel: str | None = Field(
        default=None,
        sa_column=Column(String(128), nullable=True),
        description="Business source channel such as website, event, partner, or outbound.",
    )
    source_name: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Campaign, form, import, or provider display name.",
    )
    source_ref: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="External campaign ID, form slug, import ID, or source reference.",
    )

    status: CRMLeadStatus = Field(
        default=CRMLeadStatus.NEW,
        sa_column=Column(String(32), nullable=False, server_default=text("'new'")),
        description="Lead lifecycle status.",
    )
    priority: CRMLeadPriority | None = Field(
        default=None,
        sa_column=Column(String(32), nullable=True),
        description="Queue priority used by sales workflows.",
    )
    score: float | None = Field(
        default=None,
        sa_column=Column(Numeric(8, 2), nullable=True),
        description="Lead score from product rules, enrichment, or provider sync.",
    )

    first_name: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Lead person's given name when supplied separately from full_name.",
    )
    last_name: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Lead person's family name when supplied separately from full_name.",
    )
    full_name: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Display name exactly as entered or imported for the lead person.",
    )
    email: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Primary email address used for matching, routing, and dedupe.",
    )
    phone: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
        description="Primary phone number captured from the lead source.",
    )
    job_title: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Lead person's job title or role at the company.",
    )
    linkedin_url: str | None = Field(
        default=None,
        sa_column=Column(String(500), nullable=True),
        description="LinkedIn profile URL for enrichment or sales research.",
    )

    company_name: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Company or account name associated with the lead.",
    )
    company_domain: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Normalized company domain used for account matching and dedupe.",
    )
    company_website: str | None = Field(
        default=None,
        sa_column=Column(String(500), nullable=True),
        description="Website URL captured for the lead's company.",
    )
    industry: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Industry or market segment reported for the lead's company.",
    )
    country: str | None = Field(
        default=None,
        sa_column=Column(String(128), nullable=True),
        description="Country for the lead person or company location.",
    )
    region: str | None = Field(
        default=None,
        sa_column=Column(String(128), nullable=True),
        description="State, province, or region for the lead location.",
    )
    city: str | None = Field(
        default=None,
        sa_column=Column(String(128), nullable=True),
        description="City for the lead person or company location.",
    )

    message: str | None = Field(
        default=None,
        sa_column=Column(String, nullable=True),
        description="Free-form inquiry, note, or message submitted with the lead.",
    )
    interest: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Product, service, or use case the lead is interested in.",
    )
    budget_amount: float | None = Field(
        default=None,
        sa_column=Column(Numeric(18, 2), nullable=True),
        description="Budget signal supplied before the lead becomes a deal.",
    )
    budget_currency: str | None = Field(
        default=None,
        sa_column=Column(String(3), nullable=True),
        description="ISO currency code for budget_amount.",
    )
    owner_id: UUID | None = Field(
        default=None,
        foreign_key="crm_users.user_id",
        index=True,
        description="Resolved internal CRM user currently assigned to the lead.",
    )
    owner_external_id: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Provider owner ID when the lead is synced from an external CRM.",
    )
    owner_name: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Owner display name captured before or instead of local user resolution.",
    )
    owner_email: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Owner email captured before or instead of local user resolution.",
    )
    assigned_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Timestamp when the lead was assigned to its current owner.",
    )

    contact_id: UUID | None = Field(
        default=None,
        foreign_key="crm_contacts.contact_id",
        index=True,
        description="Resolved CRM contact created from or matched to this lead.",
    )
    company_id: UUID | None = Field(
        default=None,
        foreign_key="crm_companies.company_id",
        index=True,
        description="Resolved CRM company created from or matched to this lead.",
    )

    converted_deal_id: UUID | None = Field(
        default=None,
        foreign_key="crm_deals.deal_id",
        index=True,
        description="Deal created when this lead was converted into an opportunity.",
    )
    disqualified_reason: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Reason recorded when the lead is marked unqualified.",
    )
    archive_reason: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Reason recorded when the lead is archived.",
    )

    form_id: UUID | None = Field(
        default=None,
        index=True,
        description="Product-owned lead form that captured this lead, when applicable.",
    )
    form_version_id: UUID | None = Field(
        default=None,
        index=True,
        description="Specific lead form version used at submission time.",
    )
    form_submission_id: UUID | None = Field(
        default=None,
        index=True,
        description="Submission row that produced this lead, when applicable.",
    )

    utm_source: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="UTM source value captured from the landing page or request.",
    )
    utm_medium: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="UTM medium value captured from the landing page or request.",
    )
    utm_campaign: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="UTM campaign value captured from the landing page or request.",
    )
    utm_term: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="UTM term value captured from paid search or campaign tracking.",
    )
    utm_content: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="UTM content value used to distinguish campaign variants.",
    )

    attribution: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
        description="Source attribution such as referrer, landing page, campaign, or IP metadata.",
    )
    consent: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
        description="Marketing/privacy consent captured with the lead.",
    )
    properties: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
        description="Lead custom field values keyed by crm_custom_field_definitions.key.",
    )
    metrics: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
        description="Lead scoring, enrichment, and engagement metrics.",
    )
    data_payload: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
        description="Raw source/provider/form payload for replay and troubleshooting.",
    )
    crm_url: str | None = Field(
        default=None,
        sa_column=Column(String(500), nullable=True),
        description="External CRM URL for provider-backed leads.",
    )

    source_created_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Timestamp when the lead was created in the source system.",
    )
    captured_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="Timestamp when CopilotGTM captured or created the lead.",
    )
    last_activity_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Most recent known activity timestamp for the lead.",
    )
    last_contacted_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Most recent timestamp when sales contacted the lead.",
    )
    qualified_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Timestamp when the lead reached qualified status.",
    )
    converted_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Timestamp when the lead was converted into a deal.",
    )
    disqualified_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Timestamp when the lead was marked unqualified.",
    )
    synced_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Timestamp of the last successful provider sync for this lead.",
    )
    source_modified_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Last modified timestamp reported by the source system.",
    )
    last_seen_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Timestamp when this provider-backed lead was last seen in sync.",
    )

    created_by_user_id: UUID | None = Field(
        default=None,
        foreign_key="crm_users.user_id",
        index=True,
        description="Internal CRM user who manually created the lead, when available.",
    )
    updated_by_user_id: UUID | None = Field(
        default=None,
        foreign_key="crm_users.user_id",
        index=True,
        description="Internal CRM user who last updated the lead, when available.",
    )


class CRMLeadParticipant(FlexibleSyncedRecordMixin, table=True):
    """Contact, company, owner, or custom participant linked to a lead."""

    __tablename__ = "crm_lead_participants"
    __table_args__ = (
        Index(
            "uq_crm_lead_participant_external",
            "integration_id",
            "external_id",
            unique=True,
            postgresql_where=text("integration_id IS NOT NULL AND external_id IS NOT NULL"),
        ),
        Index(
            "uq_crm_lead_participant_local_role",
            "lead_id",
            "participant_type",
            "related_local_id",
            "relationship_role",
            unique=True,
            postgresql_where=text("related_local_id IS NOT NULL AND is_deleted = false"),
        ),
        Index(
            "uq_crm_lead_participant_external_role",
            "lead_id",
            "participant_type",
            "related_external_id",
            "relationship_role",
            unique=True,
            postgresql_where=text("related_external_id IS NOT NULL AND is_deleted = false"),
        ),
        Index(
            "uq_crm_lead_participant_primary_role",
            "lead_id",
            "participant_type",
            "relationship_role",
            unique=True,
            postgresql_where=text("is_primary = true AND is_deleted = false"),
        ),
        Index("ix_crm_lead_participant_org_lead", "organisation_id", "lead_id"),
        Index(
            "ix_crm_lead_participant_org_type",
            "organisation_id",
            "participant_type",
            "is_deleted",
        ),
        Index(
            "ix_crm_lead_participant_email",
            "organisation_id",
            "email",
            postgresql_where=text("email IS NOT NULL AND is_deleted = false"),
        ),
        Index("ix_crm_lead_participant_related_local", "related_local_id"),
        CheckConstraint(
            "related_external_id IS NOT NULL OR related_local_id IS NOT NULL OR email IS NOT NULL",
            name="ck_crm_lead_participant_has_identity",
        ),
    )

    participant_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Internal UUID for the lead participant relationship.",
    )
    external_id: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Provider association/participant ID when synced from an external CRM.",
    )

    lead_id: UUID = Field(
        foreign_key="crm_leads.lead_id",
        index=True,
        description="Lead that owns this participant relationship.",
    )
    participant_type: CRMLeadParticipantType = Field(
        sa_column=Column(String(32), nullable=False),
        description="Type of participant linked to the lead.",
    )
    relationship_role: str | None = Field(
        default="participant",
        sa_column=Column(
            String(128), nullable=False, index=True, server_default=text("'participant'")
        ),
        description="Role such as primary_contact, participant, primary_company, or owner.",
    )
    is_primary: bool = Field(
        default=False,
        description="Whether this participant is primary for its role/type on the lead.",
    )

    related_external_id: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Provider ID of the related contact/company/user.",
    )
    related_local_id: UUID | None = Field(
        default=None,
        index=True,
        description="Local UUID of the related contact/company/user when resolved.",
    )

    name: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Display name for the participant when no local record is resolved.",
    )
    email: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Email address used to identify or later resolve the participant.",
    )
    phone: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
        description="Phone number captured for the participant relationship.",
    )
    job_title: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Participant's title or role at the associated company.",
    )
    company_name: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Company name for this participant when captured on the lead.",
    )
    company_domain: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Company domain for participant matching or enrichment.",
    )

    source: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
        description="Origin of the participant link, e.g. form, manual, import, provider_sync.",
    )
    source_reference_id: UUID | None = Field(
        default=None,
        index=True,
        description="Source row that produced this participant link, when applicable.",
    )

    properties: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
        description="Additional metadata for this lead participant relationship.",
    )
    data_payload: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSONB, nullable=True),
        description="Raw provider/form/import payload for the participant link.",
    )

    source_created_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Timestamp when the participant link was created in the source system.",
    )
    source_modified_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Last modified timestamp for this link in the source system.",
    )
    synced_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Timestamp of the last successful sync for this participant link.",
    )
    last_seen_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Timestamp when this participant link was last seen in provider sync.",
    )


class CRMLeadStatusHistory(SQLModel, table=True):
    """Audit row for a lead status transition."""

    __tablename__ = "crm_lead_status_history"
    __table_args__ = (
        Index("ix_crm_lead_status_history_lead", "organisation_id", "lead_id", "changed_at"),
        Index(
            "ix_crm_lead_status_history_status",
            "organisation_id",
            "to_status",
            "changed_at",
        ),
    )

    history_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Internal UUID for the lead status history row.",
    )
    organisation_id: UUID = Field(
        index=True,
        description="Organisation that owns the lead and status transition.",
    )
    lead_id: UUID = Field(
        foreign_key="crm_leads.lead_id",
        index=True,
        description="Lead whose status changed.",
    )
    from_status: CRMLeadStatus | None = Field(
        default=None,
        sa_column=Column(String(32), nullable=True),
        description="Previous lead status before the transition.",
    )
    to_status: CRMLeadStatus = Field(
        sa_column=Column(String(32), nullable=False),
        description="New lead status after the transition.",
    )
    reason: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Optional human-readable reason for the status change.",
    )
    actor_user_id: UUID | None = Field(
        default=None,
        index=True,
        description="CRM user who caused the transition, when known.",
    )
    source: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
        description="Workflow or channel that caused the transition.",
    )
    changed_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="Timestamp when the status transition was recorded.",
    )
    transition_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
        description="Additional transition context such as conversion IDs or automation metadata.",
    )


__all__ = [
    "CRMLead",
    "CRMLeadParticipant",
    "CRMLeadParticipantType",
    "CRMLeadPriority",
    "CRMLeadSourceType",
    "CRMLeadStatus",
    "CRMLeadStatusHistory",
]
