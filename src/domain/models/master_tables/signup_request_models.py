"""Durable control-plane state for provider-backed organisation signup."""

from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, UniqueConstraint, text
from sqlmodel import Field

from .base_mixins import TimestampMixin


class ExternalSignupProvider(str, Enum):
    """Work-account providers permitted to found an organisation."""

    GOOGLE_WORKSPACE = "google_workspace"
    MICROSOFT_ENTRA = "microsoft_entra"


class SignupRequestStatus(str, Enum):
    """Committed milestones used to resume cross-system provisioning safely."""

    DRAFT = "draft"
    RECEIVED = "received"
    CONTROL_PLANE_CREATED = "control_plane_created"
    TENANT_DEFAULTS_CREATED = "tenant_defaults_created"
    STORAGE_CREATED = "storage_created"
    ACTIVE = "active"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    FAILED = "failed"


class SignupRequest(TimestampMixin, table=True):
    """Persist signup intent and milestones without storing provider credentials."""

    __tablename__ = "signup_requests"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key_hash",
            name="uq_signup_idempotency_key_hash",
        ),
        UniqueConstraint(
            "provider",
            "provider_user_id",
            "provider_tenant_id",
            name="uq_signup_provider_subject_tenant",
        ),
        {"schema": "public"},
    )

    signup_request_id: UUID = Field(default_factory=uuid4, primary_key=True)
    idempotency_key_hash: str = Field(sa_column=Column(String(64), nullable=False))
    request_fingerprint: str = Field(sa_column=Column(String(64), nullable=False))
    provider: ExternalSignupProvider = Field(sa_column=Column(String(32), nullable=False))
    supabase_user_id: str = Field(sa_column=Column(String(255), nullable=False, index=True))
    provider_user_id: str = Field(sa_column=Column(String(512), nullable=False))
    provider_tenant_id: str = Field(sa_column=Column(String(512), nullable=False))
    normalized_email: str = Field(sa_column=Column(String(255), nullable=False))
    first_name: str = Field(sa_column=Column(String(100), nullable=False))
    last_name: str = Field(sa_column=Column(String(100), nullable=False))
    job_title: str | None = Field(default=None, max_length=100)
    organisation_name: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
    )
    primary_domain: str = Field(sa_column=Column(String(253), nullable=False, index=True))
    country: str | None = Field(default=None, sa_column=Column(String(2), nullable=True))
    sales_team_size: int | None = Field(default=None, ge=0, le=1_000_000)
    website_url: str | None = Field(default=None, max_length=2048)
    linkedin_url: str | None = Field(default=None, max_length=2048)
    terms_version: str | None = Field(default=None, sa_column=Column(String(32), nullable=True))
    privacy_version: str | None = Field(default=None, sa_column=Column(String(32), nullable=True))
    profile_image_url: str | None = Field(default=None, max_length=1000)
    profile_incomplete: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default=text("false")),
    )
    status: SignupRequestStatus = Field(
        default=SignupRequestStatus.DRAFT,
        sa_column=Column(String(40), nullable=False, server_default=text("'draft'"), index=True),
    )
    organisation_id: UUID | None = Field(
        default=None,
        sa_column=Column(
            ForeignKey("public.organisations.organisation_id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    user_id: UUID | None = Field(
        default=None,
        sa_column=Column(
            ForeignKey("public.users.user_id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    attempt_count: int = Field(default=0, nullable=False)
    last_error_code: str | None = Field(
        default=None,
        sa_column=Column(String(100), nullable=True),
    )
    last_error_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    completed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
