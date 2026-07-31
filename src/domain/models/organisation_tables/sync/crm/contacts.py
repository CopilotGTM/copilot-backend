from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Column, DateTime, Index, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field

from ...base_mixins import utcnow
from ..integration_models import EMPTY_JSONB_SQL
from .base import CRMSyncedRecordMixin, tenant_foreign_key


class CRMContact(CRMSyncedRecordMixin, table=True):
    """Normalised CRM contact profile."""

    __tablename__ = "crm_contacts"
    __table_args__ = (
        UniqueConstraint(
            "organisation_id",
            "contact_id",
            name="uq_crm_contacts_org_contact_id",
        ),
        tenant_foreign_key(
            "crm_contacts",
            "integration_id",
            "knowledge_integrations",
            "integration_id",
        ),
        tenant_foreign_key(
            "crm_contacts",
            "company_id",
            "crm_companies",
            "company_id",
        ),
        tenant_foreign_key(
            "crm_contacts",
            "owner_id",
            "crm_users",
            "user_id",
        ),
        Index(
            "ix_crm_contacts_org_integration_id",
            "organisation_id",
            "integration_id",
        ),
        Index("ix_crm_contacts_org_owner_id", "organisation_id", "owner_id"),
        Index("ix_crm_contact_org_email", "organisation_id", "email"),
        Index("ix_crm_contact_lifecycle", "organisation_id", "lifecycle_stage"),
        Index("ix_crm_contact_company_id", "organisation_id", "company_id"),
    )

    # Identity
    contact_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Internal UUID for the normalised contact record.",
    )
    # Core profile
    first_name: str | None = Field(default=None, description="Contact given name.")
    last_name: str | None = Field(default=None, description="Contact family name.")
    full_name: str | None = Field(default=None, description="Display name for the contact.")
    job_title: str | None = Field(
        default=None, description="Job title captured in the CRM profile."
    )
    company_name: str | None = Field(
        default=None, description="Company string stored on the contact."
    )
    # Explicit FK to the linked account — takes precedence over domain matching
    company_id: UUID | None = Field(
        default=None,
        description="Explicit FK to the linked account. When set, takes precedence over domain matching.",
    )

    # Email & communication
    email: str | None = Field(
        default=None,
        sa_column=Column(String(320), nullable=True),
        description="Primary email address for the contact.",
    )
    additional_emails: list[str] = Field(
        default_factory=list,
        sa_column=Column(
            JSONB,
            nullable=False,
            server_default=text("'[]'::jsonb"),
        ),
        description="Additional email addresses returned by the provider.",
    )
    phone: str | None = Field(default=None, description="Primary phone number.")
    mobile_phone: str | None = Field(default=None, description="Mobile phone number.")
    fax: str | None = Field(default=None, description="Fax number if supplied.")
    website: str | None = Field(default=None, description="Website associated with the contact.")
    linkedin_url: str | None = Field(
        default=None,
        sa_column=Column(String(500), nullable=True),
        description="Canonical LinkedIn profile URL for this person.",
    )

    # Owner & segmentation
    owner_email: str | None = Field(
        default=None,
        sa_column=Column(String(255)),
        description="Email address for the owner, if provided.",
    )
    owner_name: str | None = Field(
        default=None,
        sa_column=Column(String(255)),
        description="Display name for the owner, if provided.",
    )
    owner_id: UUID | None = Field(
        default=None,
        description="Resolved internal user ID for the contact owner (FK to crm_users).",
    )
    lifecycle_stage: str | None = Field(
        default=None,
        description="Lifecycle stage (e.g., subscriber, lead, opportunity).",
    )
    lead_status: str | None = Field(
        default=None,
        description="Sales lead status assigned to the contact.",
    )
    marketing_status: str | None = Field(
        default=None,
        description="Marketing email subscription status provided by the CRM.",
    )

    # Location
    address: str | None = Field(default=None, description="Street address line.")
    city: str | None = Field(default=None)
    state: str | None = Field(default=None, description="State or region value.")
    postal_code: str | None = Field(default=None, description="Postal or ZIP code.")
    country: str | None = Field(default=None)
    timezone: str | None = Field(default=None)

    # Activity
    last_contacted_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Timestamp of the last logged contact touch.",
    )
    last_activity_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Latest activity timestamp recorded for this contact.",
    )
    source_created_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Original creation time reported by the provider.",
    )
    source_modified_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Last modified timestamp reported by the provider.",
    )
    synced_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="Timestamp when this contact row was last synced.",
    )
    last_seen_at: datetime | None = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Most recent time this contact was observed during sync.",
    )
    contact_research: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Contact-level research profile for this person.",
    )
    last_researched_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Most recent time contact research was completed.",
    )
    profile_enrichment: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(
            JSONB,
            nullable=False,
            server_default=text(EMPTY_JSONB_SQL),
        ),
        description="Structured person enrichment from providers such as Apollo.",
    )
    profile_enrichment_source: str | None = Field(
        default=None,
        sa_column=Column(String(64), nullable=True),
        description="Provider or workflow that last populated structured enrichment.",
    )
    profile_enriched_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Most recent time structured profile enrichment was completed.",
    )

    # Payloads
    properties: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(
            JSONB,
            nullable=False,
            server_default=text(EMPTY_JSONB_SQL),
        ),
        description="Raw provider properties captured for this contact.",
    )
    metrics: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(
            JSONB,
            nullable=False,
            server_default=text(EMPTY_JSONB_SQL),
        ),
        description="Derived numeric fields or usage counters for analytics.",
    )
