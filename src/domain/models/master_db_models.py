import uuid
from uuid import UUID, uuid4
from typing import List, Optional
from datetime import date, datetime
from enum import Enum

from sqlmodel import SQLModel, Field, Relationship
from pydantic import EmailStr


def utcnow() -> datetime:
    return datetime.utcnow()


def today() -> date:
    return date.today()


class Organisation(SQLModel, table=True):
    __tablename__ = "organisations"

    organisation_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
    )
    organisation_name: str = Field(
        ...,
        max_length=255,
        nullable=False,
        unique=True,
    )
    schema_name: Optional[str] = Field(default=None, unique=True, index=True)
    role_name: Optional[str] = Field(default=None, unique=True, index=True)
    sales_team_size: Optional[int] = Field(default=None)
    country: Optional[str] = Field(default=None)
    company_address: Optional[str] = Field(default=None)
    linked_urls: Optional[str] = Field(default=None)
    website_urls: Optional[str] = Field(default=None)
    summary: Optional[str] = Field(default=None)
    is_active: bool = Field(default=True)
    logo_url: Optional[str] = Field(default=None, max_length=500)
    joined_date: date = Field(default_factory=today)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    created_by: Optional[UUID] = Field(default=None, foreign_key="super_admins.admin_id", index=True)
    approved_by: Optional[UUID] = Field(default=None, foreign_key="super_admins.admin_id", index=True)

    # back‑ref to users
    users: List["User"] = Relationship(back_populates="organisation")


class User(SQLModel, table=True):
    __tablename__ = "users"

    user_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
    )
    # Supabase auth user ID (from auth.users table)
    supabase_user_id: Optional[str] = Field(default=None, unique=True, index=True)
    organisation_id: UUID = Field(
        foreign_key="organisations.organisation_id",
        nullable=False,
        index=True,
    )
    first_name: str = Field(..., max_length=100)
    last_name: str = Field(..., max_length=100)
    email: str = Field(
        ...,
        max_length=255,
        nullable=False,
        unique=True,
    )
    organisation_role: str = Field(..., nullable=False)
    title: Optional[str] = Field(default=None, max_length=100)
    is_active: bool = Field(default=True)
    is_admin: bool = Field(default=False)
    last_login_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    created_by: Optional[UUID] = Field(default=None, foreign_key="super_admins.admin_id", index=True)
    organisation: Organisation = Relationship(back_populates="users")


class AdminRole(str, Enum):
    SUPER_ADMIN = "super_admin"
    ADMIN = "admin"


class AdminStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    PENDING_SETUP = "pending_setup"


class SuperAdmin(SQLModel, table=True):
    __tablename__ = "super_admins"

    admin_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
    )
    # Supabase auth user ID (from auth.users table)
    supabase_user_id: str = Field(
        ...,
        unique=True,
        index=True,
        nullable=False,
    )
    email: EmailStr = Field(
        ...,
        unique=True,
        index=True,
        nullable=False,
    )
    first_name: str = Field(..., max_length=100)
    last_name: str = Field(..., max_length=100)

    # Status and role
    role: AdminRole = Field(default=AdminRole.SUPER_ADMIN)
    status: AdminStatus = Field(default=AdminStatus.PENDING_SETUP)

    # Timestamps
    last_login_at: Optional[datetime] = Field(default=None, nullable=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    # Email verification
    email_verified: bool = Field(default=False)
