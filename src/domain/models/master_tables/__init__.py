"""
Master DB Tables
================

This package contains the SQLModel definitions for the master database, which is responsible
for storing organisation-wide data, user metadata, and super admin information.

The models are organised into separate files based on their domain:
- `organisation_models`: Manages organisations and their action history.
- `user_models`: Manages users and their action history.
- `super_admin_models`: Manages super admins and their roles.
- `base_mixins`: Provides common mixin classes for timestamps.

Each model is a SQLModel class that also serves as a Pydantic model for API validation.
Relationships between tables are defined using forward references and `Relationship` fields,
with type checking handled via `if TYPE_CHECKING:` blocks to prevent circular imports.

"""

from .base_mixins import CreatedAtMixin, TimestampMixin
from .organisation_models import Organisation, OrganisationActionHistory, OrganisationEventType, OrganisationStatus
from .super_admin_models import AdminRole, AdminUserStatus, SuperAdmin, SupabaseAdminStatus
from .user_models import SupabaseUserStatus, User, UserActionHistory, UserAppRole, UserEventType

__all__ = [
    # Base Mixins
    "TimestampMixin",
    "CreatedAtMixin",
    # Organisation Models
    "Organisation",
    "OrganisationStatus",
    "OrganisationActionHistory",
    "OrganisationEventType",
    # User Models
    "User",
    "UserAppRole",
    "SupabaseUserStatus",
    "AdminUserStatus",
    "UserActionHistory",
    "UserEventType",
    # Super Admin Models
    "SuperAdmin",
    "AdminRole",
    "SupabaseAdminStatus",
]