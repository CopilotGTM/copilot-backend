"""External Identity Mapping Models.

This module defines the ExternalIdentityMapping table for linking external identities
(Teams, Slack, SSO providers) to CopilotGTM users.

This enables authenticated experiences through chat bots and SSO providers by
maintaining a mapping between external provider user IDs and internal user records.
"""

from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlmodel import Field, Relationship

from .base_mixins import TimestampMixin

if TYPE_CHECKING:
    from .organisation_models import Organisation
    from .user_models import User


class ExternalIdentityProvider(str, Enum):
    """Supported external identity providers."""

    TEAMS = "teams"
    SLACK = "slack"
    AZURE_AD = "azure_ad"
    GOOGLE = "google"
    OKTA = "okta"


class ExternalIdentityProviderType(str, Enum):
    """Type of external identity provider."""

    CHAT_BOT = "chat_bot"
    SSO = "sso"
    OAUTH = "oauth"


class ExternalIdentityMapping(TimestampMixin, table=True):
    """Maps external provider identities to CopilotGTM users.

    This table enables:
    - Teams bot authentication (user sends message -> bot looks up linked account)
    - Slack bot authentication (future)
    - SSO silent auth (future - if user has Azure AD SSO, can auto-link Teams)

    The mapping is unique per (provider, external_user_id, external_tenant_id) to
    prevent duplicate links while allowing the same user to link multiple providers.
    """

    __tablename__ = "external_identity_mappings"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "external_user_id",
            "external_tenant_id",
            name="uq_external_identity_provider_user_tenant",
        ),
        Index("ix_external_identity_user_provider", "user_id", "provider"),
        Index("ix_external_identity_org_provider", "organisation_id", "provider"),
        Index("ix_external_identity_external_user_id", "external_user_id"),
        Index("ix_external_identity_is_active", "is_active"),
        {"schema": "public"},
    )

    # Primary key
    mapping_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Unique UUID for this identity mapping.",
    )

    # Foreign keys
    user_id: UUID = Field(
        sa_column=Column(
            ForeignKey("public.users.user_id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        description="CopilotGTM user this external identity is linked to.",
    )
    organisation_id: UUID = Field(
        sa_column=Column(
            ForeignKey("public.organisations.organisation_id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        description="Organisation this user belongs to (denormalized for query efficiency).",
    )

    # Provider identification
    provider: ExternalIdentityProvider = Field(
        sa_column=Column(String(32), nullable=False),
        description="External provider name (teams, slack, azure_ad, etc.).",
    )
    provider_type: ExternalIdentityProviderType = Field(
        sa_column=Column(String(32), nullable=False),
        description="Type of provider (chat_bot, sso, oauth).",
    )

    # External identity fields
    external_user_id: str = Field(
        sa_column=Column(String(512), nullable=False),
        description="User ID from the external provider (e.g., Teams AAD object ID).",
    )
    external_tenant_id: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True),
        description="Tenant/workspace ID from the external provider (e.g., Azure AD tenant ID).",
    )
    external_user_email: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Email address from the external provider (for display/verification).",
    )
    external_user_name: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Display name from the external provider.",
    )

    # Bot context for proactive messaging
    conversation_context: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSON, nullable=True),
        description="Bot conversation context for proactive messaging (service_url, conversation_id, etc.).",
    )

    # Status and timestamps
    is_active: bool = Field(
        default=True,
        sa_column=Column(Boolean, nullable=False, server_default="true"),
        description="Whether this identity link is active. Set to false on logout.",
    )
    linked_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="When this identity was linked.",
    )
    last_used_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Last time this identity was used for authentication.",
    )

    # Relationships
    user: "User" = Relationship(back_populates="external_identities")
    organisation: "Organisation" = Relationship(back_populates="external_identity_mappings")
