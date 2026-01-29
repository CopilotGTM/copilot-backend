from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pydantic import EmailStr
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
from sqlmodel import Field, Relationship

from .base_mixins import CreatedAtMixin, TimestampMixin
from .super_admin_models import AdminUserStatus

if TYPE_CHECKING:
    from .external_identity_models import ExternalIdentityMapping
    from .organisation_models import Organisation
    from .super_admin_models import SuperAdmin


# Enums for App/User domain
class UserAppRole(str, Enum):
    """Application-level user role (member/admin)."""

    MEMBER = "member"
    ADMIN = "admin"


class SupabaseUserStatus(str, Enum):
    """Supabase authentication status for user accounts."""

    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    PENDING_SETUP = "PASSWORD_CHANGE_REQUIRED"


class UserEventType(str, Enum):
    """Enumerates user history events."""

    CREATED = "created"
    UPDATED = "updated"
    STATUS_CHANGED = "status_changed"
    ROLE_CHANGED = "role_changed"
    DELETED = "deleted"
    LOGIN = "login"
    PASSWORD_RESET = "password_reset"
    EMAIL_VERIFIED = "email_verified"
    FIELD_CHANGED = "field_changed"


class User(TimestampMixin, table=True):
    __tablename__ = "users"
    __table_args__ = {"schema": "public"}

    user_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Unique UUID for the user record.",
    )
    supabase_user_id: str | None = Field(
        default=None,
        unique=True,
        index=True,
        description="Unique user ID from Supabase authentication provider.",
    )
    organisation_id: UUID = Field(
        foreign_key="public.organisations.organisation_id",
        nullable=False,
        index=True,
        description="UUID of the organisation this user belongs to.",
    )
    first_name: str = Field(..., max_length=100, description="User's first name.")
    last_name: str = Field(..., max_length=100, description="User's last name.")
    email: EmailStr = Field(
        ...,
        unique=True,
        max_length=255,
        description="User's primary email address. Must be unique.",
    )
    user_role: UserAppRole = Field(
        ..., nullable=False, description="Application-level role (MEMBER, ADMIN, etc.)."
    )
    organisation_role: str = Field(
        ...,
        max_length=100,
        description="User's job designation within the organisation (e.g., Senior Software Engineer).",
    )
    job_title: str | None = Field(
        default=None, max_length=100, description="Optional job title for the user."
    )
    status: SupabaseUserStatus = Field(
        default=SupabaseUserStatus.PENDING_SETUP,
        index=True,
        description="Supabase authentication status (active/inactive/pending_setup). Reflects the actual state of the Supabase account.",
    )
    admin_status: AdminUserStatus = Field(
        default=AdminUserStatus.PENDING,
        index=True,
        description="Admin approval status (pending/approved/rejected/suspended). Indicates whether an admin has approved the user for access.",
    )
    email_verified: bool = Field(
        default=False,
        nullable=False,
        description="Indicates if the user's email address has been verified.",
    )
    last_login_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Timestamp of the user's most recent login. Null if never logged in.",
    )
    created_by_auth_user_id: str | None = Field(
        default=None,
        nullable=True,
        index=True,
        description="Supabase auth.users.id of the admin who created the user.",
    )
    approved_by: UUID | None = Field(
        default=None,
        sa_column=Column(
            "approved_by",
            ForeignKey("public.super_admins.admin_id", ondelete="SET NULL"),
            nullable=True,
        ),
        description="UUID of the admin who approved the user, if applicable.",
    )
    approved_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Timestamp when the user was approved by an admin.",
    )

    # Relationships
    organisation: "Organisation" = Relationship(back_populates="users")
    action_history: list["UserActionHistory"] = Relationship(back_populates="user")
    external_identities: list["ExternalIdentityMapping"] = Relationship(
        back_populates="user", cascade_delete=True
    )


class UserActionHistory(CreatedAtMixin, table=True):
    __tablename__ = "user_action_history"
    __table_args__ = (
        Index("ix_uah_user_created_at", "user_id", "created_at"),
        Index("ix_uah_action_created_at", "action", "created_at"),
        Index("ix_uah_actor_created_at", "actor_admin_id", "created_at"),
        UniqueConstraint("user_id", "request_id", name="uq_uah_user_request_id"),
        {"schema": "public"},
    )

    id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Unique UUID for this action history record.",
    )
    user_id: UUID = Field(
        foreign_key="public.users.user_id",
        nullable=False,
        index=True,
        description="User UUID this action is associated with.",
    )
    actor_admin_id: UUID | None = Field(
        default=None,
        sa_column=Column(
            ForeignKey("public.super_admins.admin_id", ondelete="SET NULL"), index=True
        ),
        description="UUID of the admin who performed (or initiated) the action.",
    )
    request_id: str | None = Field(
        default=None,
        sa_column=Column(String(64), index=True),
        description="Client request ID for idempotency/debugging.",
    )
    action: UserEventType = Field(
        ...,
        index=True,
        description="Type of user action/event recorded (created, updated, etc.).",
    )
    note: str | None = Field(
        default=None,
        sa_column=Column(Text),
        description="Optional human-readable note for this action event.",
    )
    details: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSON),
        description="Arbitrary structured data or metadata about the action (JSON).",
    )

    # Relationships
    user: "User" = Relationship(back_populates="action_history")
    actor_admin: "SuperAdmin" = Relationship(back_populates="user_action_history")
