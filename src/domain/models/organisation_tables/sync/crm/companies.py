"""Normalised CRM company/account record models."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    Numeric,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field

from ...base_mixins import utcnow
from ..base import SyncedRecordMixin
from ..integration_models import EMPTY_JSONB_SQL


class CRMCompany(SyncedRecordMixin, table=True):
    """Normalised CRM company/account record."""

    __tablename__ = "crm_companies"
    __table_args__ = (
        Index("ix_crm_company_org_domain", "organisation_id", "domain"),
        Index("ix_crm_company_org_name", "organisation_id", "name"),
        Index("ix_crm_company_lifecycle", "organisation_id", "lifecycle_stage"),
        Index("ix_crm_company_synced", "organisation_id", "synced_at"),
    )
    # Identity
    company_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Internal UUID for the normalised company record.",
    )
    # Core details
    name: str | None = Field(default=None, description="Company name as stored in the CRM.")
    domain: str | None = Field(default=None, description="Primary web domain for the company.")
    website: str | None = Field(default=None, description="Company website URL.")
    description: str | None = Field(default=None, description="Company description or notes.")
    industry: str | None = Field(default=None, description="Industry classification.")
    company_type: str | None = Field(default=None, description="Company type/segment value.")
    lifecycle_stage: str | None = Field(
        default=None, description="Lifecycle stage for the account."
    )

    # Owner information
    owner_email: str | None = Field(default=None, description="Email of the account owner.")
    owner_name: str | None = Field(default=None, description="Display name of the account owner.")
    owner_id: UUID | None = Field(
        default=None,
        foreign_key="crm_users.user_id",
        description="Resolved internal user ID for the company owner (FK to crm_users).",
    )

    # Business metrics
    number_of_employees: int | None = Field(
        default=None,
        sa_column=Column(Integer, nullable=True),
        description="Number of employees in the company.",
    )
    annual_revenue: float | None = Field(
        default=None,
        sa_column=Column(Numeric(precision=20, scale=2), nullable=True),
        description="Annual revenue of the company.",
    )

    # Contact info
    phone: str | None = Field(default=None, description="Company phone number.")
    city: str | None = Field(default=None)
    state: str | None = Field(default=None)
    country: str | None = Field(default=None)
    address: str | None = Field(default=None)
    postal_code: str | None = Field(default=None)
    timezone: str | None = Field(default=None, description="Company timezone.")

    # Social
    linkedin_url: str | None = Field(default=None, description="LinkedIn company page URL.")

    # Flags
    is_active: bool = Field(
        default=True,
        sa_column=Column(Boolean, nullable=False, server_default=text("true")),
        description="Whether the company is active according to the provider.",
    )
    # Timing
    source_created_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Original creation timestamp from the provider.",
    )
    source_modified_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Last modified timestamp from the provider.",
    )
    synced_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="Timestamp when this company row was last synced.",
    )
    last_seen_at: datetime | None = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Most recent time this company was observed during sync.",
    )
    # Payloads
    properties: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(
            JSONB,
            nullable=False,
            server_default=text(EMPTY_JSONB_SQL),
        ),
        description="Raw provider properties captured for this company.",
    )
    metrics: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(
            JSONB,
            nullable=False,
            server_default=text(EMPTY_JSONB_SQL),
        ),
        description="Derived metrics for analytics (e.g., ARR, deal counts).",
    )
