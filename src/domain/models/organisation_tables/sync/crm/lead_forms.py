"""CRM lead-form models for tenant-owned form configuration and submissions."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, ClassVar
from uuid import UUID, uuid4

from sqlalchemy import Boolean, Column, DateTime, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field

from ...base_mixins import utcnow
from ..integration_models import EMPTY_JSONB_SQL
from .base import CRMFlexibleSyncedRecordMixin, tenant_foreign_key


class LeadFormStatus(str, Enum):
    """Runtime availability for public lead capture."""

    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class LeadFormFieldTargetType(str, Enum):
    """Where an incoming form field should be written."""

    LEAD_FIELD = "lead_field"
    PROPERTY = "property"
    IGNORE = "ignore"


class LeadFormFieldDataType(str, Enum):
    """Basic browser-submitted value coercion types."""

    TEXT = "text"
    EMAIL = "email"
    PHONE = "phone"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    JSON = "json"


class LeadForm(CRMFlexibleSyncedRecordMixin, table=True):
    """Tenant-owned public lead capture form configuration."""

    __tablename__: ClassVar[str] = "lead_forms"
    __table_args__ = (
        UniqueConstraint(
            "organisation_id",
            "form_id",
            name="uq_lead_forms_org_form_id",
        ),
        tenant_foreign_key(
            "lead_forms",
            "integration_id",
            "knowledge_integrations",
            "integration_id",
        ),
        tenant_foreign_key(
            "lead_forms",
            "created_by_user_id",
            "crm_users",
            "user_id",
        ),
        tenant_foreign_key(
            "lead_forms",
            "updated_by_user_id",
            "crm_users",
            "user_id",
        ),
        Index(
            "ix_lead_forms_org_integration_id",
            "organisation_id",
            "integration_id",
        ),
        Index(
            "ix_lead_forms_org_created_by_user_id",
            "organisation_id",
            "created_by_user_id",
        ),
        Index(
            "ix_lead_forms_org_updated_by_user_id",
            "organisation_id",
            "updated_by_user_id",
        ),
        Index("ix_lead_form_org_status", "organisation_id", "status", "is_deleted"),
    )

    form_id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(sa_column=Column(String(255), nullable=False))
    description: str | None = Field(default=None, sa_column=Column(String(1000), nullable=True))
    status: LeadFormStatus = Field(
        default=LeadFormStatus.DRAFT,
        sa_column=Column(String(32), nullable=False, server_default=text("'draft'")),
    )
    current_version: int = Field(default=1, nullable=False)
    settings: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
    )
    created_by_user_id: UUID | None = Field(default=None)
    updated_by_user_id: UUID | None = Field(default=None)


class LeadFormField(CRMFlexibleSyncedRecordMixin, table=True):
    """Mapping from a client website field key to a lead column or property."""

    __tablename__: ClassVar[str] = "lead_form_fields"
    __table_args__ = (
        tenant_foreign_key(
            "lead_form_fields",
            "integration_id",
            "knowledge_integrations",
            "integration_id",
        ),
        tenant_foreign_key(
            "lead_form_fields",
            "form_id",
            "lead_forms",
            "form_id",
        ),
        Index(
            "uq_lead_form_field_key_active_org",
            "organisation_id",
            "form_id",
            "external_key",
            "is_deleted",
            unique=True,
        ),
        Index(
            "ix_lead_form_fields_org_integration_id",
            "organisation_id",
            "integration_id",
        ),
        Index("ix_lead_form_fields_org_form_id", "organisation_id", "form_id"),
        Index("ix_lead_form_field_form_order", "form_id", "display_order"),
    )

    field_id: UUID = Field(default_factory=uuid4, primary_key=True)
    form_id: UUID = Field(index=True)
    form_version: int = Field(default=1, nullable=False)
    external_key: str = Field(sa_column=Column(String(128), nullable=False))
    label: str = Field(sa_column=Column(String(255), nullable=False))
    target_type: LeadFormFieldTargetType = Field(
        default=LeadFormFieldTargetType.PROPERTY,
        sa_column=Column(String(32), nullable=False),
    )
    target_key: str | None = Field(default=None, sa_column=Column(String(128), nullable=True))
    data_type: LeadFormFieldDataType = Field(
        default=LeadFormFieldDataType.TEXT,
        sa_column=Column(String(32), nullable=False),
    )
    required: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    display_order: int | None = Field(default=None)
    default_value: Any | None = Field(default=None, sa_column=Column(JSONB, nullable=True))
    validation: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
    )


class LeadFormSubmission(CRMFlexibleSyncedRecordMixin, table=True):
    """Audit trail for a public form submission that created a lead."""

    __tablename__: ClassVar[str] = "lead_form_submissions"
    __table_args__ = (
        tenant_foreign_key(
            "lead_form_submissions",
            "integration_id",
            "knowledge_integrations",
            "integration_id",
        ),
        tenant_foreign_key(
            "lead_form_submissions",
            "form_id",
            "lead_forms",
            "form_id",
        ),
        tenant_foreign_key(
            "lead_form_submissions",
            "lead_id",
            "crm_leads",
            "lead_id",
        ),
        Index(
            "ix_lead_form_submissions_org_integration_id",
            "organisation_id",
            "integration_id",
        ),
        Index(
            "ix_lead_form_submissions_org_lead_id",
            "organisation_id",
            "lead_id",
        ),
        Index("ix_lead_form_submission_form_received", "organisation_id", "form_id", "received_at"),
        Index("ix_lead_form_submission_lead", "lead_id"),
        # Store idempotency state separately from payload JSON so private-form retries
        # can do an exact lookup instead of JSON-path matching on every retry.
        Index(
            "uq_lead_form_submission_private_idempotency",
            "organisation_id",
            "form_id",
            "ingestion_key_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("ingestion_key_id IS NOT NULL AND idempotency_key IS NOT NULL"),
        ),
        Index(
            "uq_lead_form_submission_public_idempotency",
            "organisation_id",
            "form_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("ingestion_key_id IS NULL AND idempotency_key IS NOT NULL"),
        ),
    )

    submission_id: UUID = Field(default_factory=uuid4, primary_key=True)
    form_id: UUID = Field(index=True)
    form_version: int = Field(nullable=False)
    lead_id: UUID | None = Field(default=None, index=True)
    # Persisted replay keys let private-form clients recover deterministic state on
    # retry by matching on a concrete org/form/key/token tuple instead of JSON.
    ingestion_key_id: str | None = Field(
        default=None,
        sa_column=Column(String(128), nullable=True),
        description="Form-scoped private key id used as the replay scope.",
    )
    idempotency_key: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Client replay token for mapped form submissions.",
    )
    raw_payload: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
    )
    mapped_payload: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
    )
    context: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False, server_default=text(EMPTY_JSONB_SQL)),
    )
    submitter_ip: str | None = Field(default=None, sa_column=Column(String(64), nullable=True))
    user_agent: str | None = Field(default=None, sa_column=Column(String(1000), nullable=True))
    received_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


__all__ = [
    "LeadForm",
    "LeadFormField",
    "LeadFormFieldDataType",
    "LeadFormFieldTargetType",
    "LeadFormStatus",
    "LeadFormSubmission",
]
