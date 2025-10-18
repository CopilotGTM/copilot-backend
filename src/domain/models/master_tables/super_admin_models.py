from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from pydantic import EmailStr
from sqlalchemy import Column, DateTime
from sqlmodel import Field, Relationship

from .base_mixins import TimestampMixin

if TYPE_CHECKING:
    from .organisation_models import OrganisationActionHistory
    from .user_models import UserActionHistory


class AdminRole(str, Enum):
    """Role type of the super admin (SUPER_ADMIN or ADMIN)."""

    SUPER_ADMIN = "super_admin"
    ADMIN = "admin"


class SupabaseAdminStatus(str, Enum):
    """Supabase authentication status for the super admin account."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    PENDING_SETUP = "pending_setup"


class AdminUserStatus(str, Enum):
    """Admin-related approval (pending/approved/rejected/suspended)."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUSPENDED = "suspended"
    DEACTIVATED = "deactivated"


class SuperAdmin(TimestampMixin, table=True):
    __tablename__ = "super_admins"
    __table_args__ = {"schema": "public"}

    admin_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Unique UUID for the super admin record.",
    )
    supabase_user_id: str = Field(
        ...,
        unique=True,
        index=True,
        nullable=False,
        description="Unique user ID from Supabase authentication provider for the super admin.",
    )
    email: EmailStr = Field(
        ...,
        unique=True,
        index=True,
        nullable=False,
        description="Super admin's email address (must be unique).",
    )
    first_name: str = Field(..., max_length=100, description="Super admin's first name.")
    last_name: str = Field(..., max_length=100, description="Super admin's last name.")
    role: AdminRole = Field(
        default=AdminRole.SUPER_ADMIN,
        description="Role type of the super admin (SUPER_ADMIN or ADMIN).",
    )
    status: SupabaseAdminStatus = Field(
        default=SupabaseAdminStatus.PENDING_SETUP,
        description="Supabase authentication status for the super admin account (active/inactive/pending_setup).",
    )
    admin_status: AdminUserStatus = Field(
        default=AdminUserStatus.PENDING,
        index=True,
        description="Admin approval status (pending/approved/rejected/suspended). Determines if the super admin has been approved by other admins.",
    )
    last_login_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Timestamp of the super admin's most recent login. Null if never logged in.",
    )
    email_verified: bool = Field(
        default=False,
        description="Whether the super admin's email has been verified in Supabase.",
    )

    # Relationships
    action_history: list["OrganisationActionHistory"] = Relationship(back_populates="actor_admin")
    user_action_history: list["UserActionHistory"] = Relationship(back_populates="actor_admin")
