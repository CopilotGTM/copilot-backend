from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlmodel import Field, Relationship

from .base_mixins import CreatedAtMixin, TimestampMixin

if TYPE_CHECKING:
    from .bot_platform_installation_models import BotPlatformInstallation
    from .external_identity_models import ExternalIdentityMapping
    from .super_admin_models import SuperAdmin
    from .user_models import User


class OrganisationStatus(str, Enum):
    """Status enum for lifecycle of organisation."""

    PROVISIONING = "provisioning"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUSPENDED = "suspended"
    DEACTIVATED = "deactivated"
    FAILED = "failed"


class OrganisationEventType(str, Enum):
    """Enumerates major organisation events."""

    CREATED = "created"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUSPENDED = "suspended"
    REINSTATED = "reinstated"
    STATUS_CHANGED = "status_changed"
    FIELD_CHANGED = "field_changed"
    USER_ADDED = "user_added"
    USER_REMOVED = "user_removed"
    ROLE_CHANGED = "role_changed"
    NOTE_ADDED = "note_added"
    SYSTEM_MIGRATION = "system_migration"
    DELETED = "deleted"


class Organisation(TimestampMixin, table=True):
    __tablename__ = "organisations"
    __table_args__ = {"schema": "public"}

    organisation_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Unique UUID for the organisation.",
    )
    organisation_name: str = Field(
        sa_column=Column(String(255), nullable=False, unique=True),
        description="Organisation/company name. Unique.",
    )
    domain: list[str] | None = Field(
        default=None,
        sa_column=Column(JSON, nullable=True),
        description="List of primary domains of the organisation (e.g. ['acme.com', 'acme.io']).",
    )
    agent_name: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Name of the organisation to be used in Agent contexts.",
    )
    sales_team_size: int | None = Field(
        default=None,
        ge=0,
        le=1_000_000,
        description="Integer headcount for the organisation's sales team.",
    )
    country: str | None = Field(
        default=None,
        description="Primary country of registration or operation for the organisation.",
    )
    company_address: str | None = Field(
        default=None,
        description="Physical or mailing address for the company/organisation.",
    )
    linked_urls: list[str] | None = Field(
        default=None,
        sa_column=Column(JSON),
        description="List of linked URLs related to the organisation (JSON format).",
    )
    website_urls: list[str] | None = Field(
        default=None,
        sa_column=Column(JSON),
        description="List of primary website URLs for the organisation (JSON format).",
    )
    summary: str | None = Field(
        default=None,
        sa_column=Column(Text),
        description="Freeform summary or overview about the organisation.",
    )
    is_active: bool = Field(
        default=True,
        description="Status flag. True if the organisation is considered active.",
    )
    logo_url: str | None = Field(
        default=None,
        max_length=500,
        description="URL pointing to the organisation's logo (if set).",
    )
    status: OrganisationStatus = Field(
        sa_column=Column(
            SAEnum(OrganisationStatus),
            default=OrganisationStatus.PROVISIONING,
            index=True,
        ),
        description="Lifecycle status for the organisation (provisioning, pending, approved, etc.).",
    )
    created_by_auth_user_id: str | None = Field(
        default=None,
        nullable=True,
        index=True,
        description="Supabase auth.users.id of the user who created the organisation.",
    )
    approved_by: UUID | None = Field(
        default=None,
        sa_column=Column(
            "approved_by",
            ForeignKey("public.super_admins.admin_id", ondelete="SET NULL"),
            nullable=True,
        ),
        description="UUID of the admin who approved the organisation.",
    )
    approved_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Timestamp when the organisation was approved.",
    )

    # Relationships
    users: list["User"] = Relationship(back_populates="organisation", cascade_delete=True)
    action_history: list["OrganisationActionHistory"] = Relationship(back_populates="organisation")
    external_identity_mappings: list["ExternalIdentityMapping"] = Relationship(
        back_populates="organisation", cascade_delete=True
    )
    bot_platform_installations: list["BotPlatformInstallation"] = Relationship(
        back_populates="organisation"
    )


class OrganisationActionHistory(CreatedAtMixin, table=True):
    __tablename__ = "organisation_action_history"
    __table_args__ = (
        Index("ix_oah_org_created_at", "organisation_id", "created_at"),
        Index("ix_oah_action_created_at", "action", "created_at"),
        Index("ix_oah_actor_created_at", "actor_admin_id", "created_at"),
        UniqueConstraint("organisation_id", "request_id", name="uq_oah_org_request_id"),
        {"schema": "public"},
    )

    id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Unique UUID for this organisation action history record.",
    )
    organisation_id: UUID = Field(
        foreign_key="public.organisations.organisation_id",
        nullable=False,
        index=True,
        description="UUID of the organisation this action references.",
    )
    actor_admin_id: UUID | None = Field(
        default=None,
        sa_column=Column(
            ForeignKey("public.super_admins.admin_id", ondelete="SET NULL"), index=True
        ),
        description="UUID of admin who performed this organisation action.",
    )
    request_id: str | None = Field(
        default=None,
        sa_column=Column(String(64), index=True),
        description="Client request ID for this action (idempotency/debug).",
    )
    action: OrganisationEventType = Field(
        ...,
        index=True,
        description="Type of organisation event recorded (created, approved, suspended, etc.).",
    )
    note: str | None = Field(
        default=None,
        sa_column=Column(Text),
        description="Optional comment or note for this action.",
    )
    details: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSON),
        description="Additional structured data or metadata for the action (JSON format).",
    )

    # Relationships
    organisation: "Organisation" = Relationship(back_populates="action_history")
    actor_admin: "SuperAdmin" = Relationship(back_populates="action_history")
