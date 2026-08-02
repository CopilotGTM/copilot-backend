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
from .bot_platform_installation_models import (
    BotPlatformInstallation,
    BotPlatformProvider,
)
from .external_identity_models import (
    ExternalIdentityMapping,
    ExternalIdentityProvider,
    ExternalIdentityProviderType,
)
from .organisation_integration_models import OrganisationIntegrationDetails
from .organisation_job_models import (
    OrganisationJob,
    OrganisationJobStatus,
    OrganisationJobType,
)
from .organisation_models import (
    Organisation,
    OrganisationActionHistory,
    OrganisationEventType,
    OrganisationStatus,
)
from .public_ingress_key_models import (
    PublicIngressKey,
    PublicIngressKeyStatus,
    PublicIngressKeyType,
    PublicIngressResourceType,
)
from .signup_request_models import (
    ExternalSignupProvider,
    SignupRequest,
    SignupRequestStatus,
)
from .super_admin_models import (
    AdminRole,
    AdminUserStatus,
    SupabaseAdminStatus,
    SuperAdmin,
)
from .user_models import (
    SupabaseUserStatus,
    User,
    UserActionHistory,
    UserAppRole,
    UserAuthenticationMethod,
    UserEventType,
)

__all__ = [
    # Base Mixins
    "TimestampMixin",
    "CreatedAtMixin",
    # Organisation Models
    "Organisation",
    "OrganisationStatus",
    "OrganisationActionHistory",
    "OrganisationEventType",
    "OrganisationIntegrationDetails",
    "PublicIngressKey",
    "PublicIngressKeyStatus",
    "PublicIngressKeyType",
    "PublicIngressResourceType",
    # Organisation Job Models
    "OrganisationJob",
    "OrganisationJobStatus",
    "OrganisationJobType",
    "SignupRequest",
    "SignupRequestStatus",
    "ExternalSignupProvider",
    # User Models
    "User",
    "UserAppRole",
    "UserAuthenticationMethod",
    "SupabaseUserStatus",
    "AdminUserStatus",
    "UserActionHistory",
    "UserEventType",
    # Super Admin Models
    "SuperAdmin",
    "AdminRole",
    "SupabaseAdminStatus",
    # External Identity Models
    "ExternalIdentityMapping",
    "ExternalIdentityProvider",
    "ExternalIdentityProviderType",
    # Bot Platform Installation Models
    "BotPlatformInstallation",
    "BotPlatformProvider",
]
