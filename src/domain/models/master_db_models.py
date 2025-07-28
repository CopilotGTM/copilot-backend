from uuid import UUID, uuid4
from typing import List, Optional, Dict, Any
from datetime import date, datetime, timezone
from enum import Enum

from sqlmodel import SQLModel, Field, Relationship
from sqlalchemy import Column, DateTime, func, ForeignKey, String, Text, JSON, Index, UniqueConstraint
from pydantic import EmailStr


# --- Time helpers ------------------------------------------------------------

def utcnow() -> datetime:
    """Return timezone-aware UTC datetime"""
    return datetime.now(timezone.utc)

def today() -> date:
    return date.today()


# --- Mixins ------------------------------------------------------------------

class CreatedAtMixin(SQLModel):
    created_at: datetime = Field(
        default=None,
        nullable=False,
        index=True,
        sa_column_kwargs={"server_default": func.now()},
        sa_type=DateTime(timezone=True),
    )

class TimestampMixin(CreatedAtMixin):
    """For main entities where you want updated_at as well."""
    updated_at: datetime = Field(
        default=None,
        nullable=False,
        index=True,
        sa_column_kwargs={"server_default": func.now(), "onupdate": func.now()},
        sa_type=DateTime(timezone=True),
    )


# --- Core enums ---------------------------------------------------------------

class OrganisationStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUSPENDED = "suspended"


# --- Tables ------------------------------------------------------------------

class Organisation(TimestampMixin, table=True):
    __tablename__ = "organisations"

    organisation_id: UUID = Field(default_factory=uuid4, primary_key=True)
    organisation_name: str = Field(sa_type=String(255), nullable=False, unique=True)
    schema_name: Optional[str] = Field(default=None, unique=True, index=True)
    role_name: Optional[str] = Field(default=None, unique=True, index=True)
    sales_team_size: Optional[int] = Field(default=None, ge=0, le=1_000_000)
    country: Optional[str] = Field(default=None)
    company_address: Optional[str] = Field(default=None)
    linked_urls: Optional[List[str]] = Field(default=None, sa_column=Column(JSON))
    website_urls: Optional[List[str]] = Field(default=None, sa_column=Column(JSON))
    summary: Optional[str] = Field(default=None, sa_column=Column(Text))
    is_active: bool = Field(default=True)
    logo_url: Optional[str] = Field(default=None, max_length=500)

    # Status + cached approval info
    status: OrganisationStatus = Field(default=OrganisationStatus.PENDING, index=True)
    created_by_auth_user_id: Optional[str] = Field(default=None, nullable=True, index=True, description="auth.users.id of creator")
    approved_by: Optional[UUID] = Field(
        default=None,
        sa_column=Column("approved_by", ForeignKey("super_admins.admin_id", ondelete="SET NULL"), nullable=True)
    )
    approved_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))

    # Relationships
    users: List["User"] = Relationship(back_populates="organisation", cascade_delete=True)
    action_history: List["OrganisationActionHistory"] = Relationship(back_populates="organisation")


class User(TimestampMixin, table=True):
    __tablename__ = "users"

    user_id: UUID = Field(default_factory=uuid4, primary_key=True)
    supabase_user_id: Optional[str] = Field(default=None, unique=True, index=True)
    organisation_id: UUID = Field(foreign_key="organisations.organisation_id", nullable=False, index=True)
    first_name: str = Field(..., max_length=100)
    last_name: str = Field(..., max_length=100)
    email: EmailStr = Field(..., unique=True, max_length=255)
    organisation_role: str = Field(..., nullable=False)
    title: Optional[str] = Field(default=None, max_length=100)
    is_active: bool = Field(default=True)
    is_admin: bool = Field(default=False)
    last_login_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    created_by_auth_user_id: Optional[str] = Field(default=None, nullable=True, index=True, description="Supabase auth.users.id of creator")

    # Relationships
    organisation: Organisation = Relationship(back_populates="users")


class AdminRole(str, Enum):
    SUPER_ADMIN = "super_admin"
    ADMIN = "admin"

class AdminStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    PENDING_SETUP = "pending_setup"


class SuperAdmin(TimestampMixin, table=True):
    __tablename__ = "super_admins"

    admin_id: UUID = Field(default_factory=uuid4, primary_key=True)
    supabase_user_id: str = Field(..., unique=True, index=True, nullable=False)
    email: EmailStr = Field(..., unique=True, index=True, nullable=False)
    first_name: str = Field(..., max_length=100)
    last_name: str = Field(..., max_length=100)
    role: AdminRole = Field(default=AdminRole.SUPER_ADMIN)
    status: AdminStatus = Field(default=AdminStatus.PENDING_SETUP)
    last_login_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    email_verified: bool = Field(default=False)

    # Relationships
    action_history: List["OrganisationActionHistory"] = Relationship(back_populates="actor_admin")


class OrganisationEventType(str, Enum):
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


class OrganisationActionHistory(CreatedAtMixin, table=True):
    __tablename__ = "organisation_action_history"
    __table_args__ = (
        # Fast common queries
        Index("ix_oah_org_created_at", "organisation_id", "created_at"),
        Index("ix_oah_action_created_at", "action", "created_at"),
        Index("ix_oah_actor_created_at", "actor_admin_id", "created_at"),
        # Idempotency per organisation (allows multiple NULLs)
        UniqueConstraint("organisation_id", "request_id", name="uq_oah_org_request_id"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    organisation_id: UUID = Field(foreign_key="organisations.organisation_id", nullable=False, index=True)

    # Who did it
    actor_admin_id: Optional[UUID] = Field(
        default=None,
        sa_column=Column(ForeignKey("super_admins.admin_id", ondelete="SET NULL"), index=True),
    )

    # Optional idempotency/debug
    request_id: Optional[str] = Field(default=None, sa_column=Column(String(64), index=True))

    # What happened
    action: OrganisationEventType = Field(..., index=True)
    note: Optional[str] = Field(default=None, sa_column=Column(Text))

    # Minimal structure so you can query without parsing text
    details: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSON))

    # Relationships
    organisation: "Organisation" = Relationship(back_populates="action_history")
    actor_admin: "SuperAdmin" = Relationship(back_populates="action_history")


# # Utility functions for audit operations
# def get_admin_by_auth_id(session, auth_user_id: str) -> Optional[SuperAdmin]:
#     """Get admin by Supabase auth user ID"""
#     return session.exec(
#         select(SuperAdmin).where(SuperAdmin.supabase_user_id == auth_user_id)
#     ).first()

# def get_user_by_auth_id(session, auth_user_id: str) -> Optional[User]:
#     """Get user by Supabase auth user ID"""
#     return session.exec(
#         select(User).where(User.supabase_user_id == auth_user_id)
#     ).first()
