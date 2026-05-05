"""CRM lead-form models for tenant-owned form configuration and submissions."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, ClassVar
from uuid import UUID, uuid4

from sqlalchemy import Boolean, Column, DateTime, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field

from ...base_mixins import utcnow
from ..base import FlexibleSyncedRecordMixin
from ..integration_models import EMPTY_JSONB_SQL


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


class LeadForm(FlexibleSyncedRecordMixin, table=True):
    """Tenant-owned public lead capture form configuration."""

    __tablename__: ClassVar[str] = "lead_forms"
    __table_args__ = (Index("ix_lead_form_org_status", "organisation_id", "status", "is_deleted"),)

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
    created_by_user_id: UUID | None = Field(default=None, foreign_key="crm_users.user_id")
    updated_by_user_id: UUID | None = Field(default=None, foreign_key="crm_users.user_id")


class LeadFormField(FlexibleSyncedRecordMixin, table=True):
    """Mapping from a client website field key to a lead column or property."""

    __tablename__: ClassVar[str] = "lead_form_fields"
    __table_args__ = (
        Index(
            "uq_lead_form_field_key_active",
            "form_id",
            "external_key",
            "is_deleted",
            unique=True,
        ),
        Index("ix_lead_form_field_form_order", "form_id", "display_order"),
    )

    field_id: UUID = Field(default_factory=uuid4, primary_key=True)
    form_id: UUID = Field(foreign_key="lead_forms.form_id", index=True)
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


class LeadFormSubmission(FlexibleSyncedRecordMixin, table=True):
    """Audit trail for a public form submission that created a lead."""

    __tablename__: ClassVar[str] = "lead_form_submissions"
    __table_args__ = (
        Index("ix_lead_form_submission_form_received", "organisation_id", "form_id", "received_at"),
        Index("ix_lead_form_submission_lead", "lead_id"),
    )

    submission_id: UUID = Field(default_factory=uuid4, primary_key=True)
    form_id: UUID = Field(foreign_key="lead_forms.form_id", index=True)
    form_version: int = Field(nullable=False)
    lead_id: UUID | None = Field(default=None, foreign_key="crm_leads.lead_id", index=True)
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
