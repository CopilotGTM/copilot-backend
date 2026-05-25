"""Bot Platform Installation Models.

This module defines the bot_platform_installations table for tracking
per-workspace/per-tenant bot installations across platforms (Slack, Teams,
Google Chat, etc.).

Each row represents one workspace's installation of the bot, storing
the credentials needed to send messages back to that workspace.

This is separate from external_identity_mappings, which tracks per-USER
identity linking. This table tracks per-WORKSPACE installation.
"""

from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlmodel import Field, Relationship

from .base_mixins import TimestampMixin

if TYPE_CHECKING:
    from .organisation_models import Organisation


class BotPlatformProvider(str, Enum):
    """Supported bot platform providers."""

    TEAMS = "teams"
    SLACK = "slack"
    WHATSAPP = "whatsapp"
    GOOGLE_CHAT = "google_chat"


class BotPlatformInstallation(TimestampMixin, table=True):
    """Tracks per-workspace bot installations across platforms.

    When a workspace admin installs the bot (e.g. Slack OAuth install flow),
    a row is created here with the workspace's bot token and metadata.

    For Teams: bot_token is NULL (Bot Framework handles auth with single app credential).
               Stores service_url in metadata for proactive messaging.
    For Slack: bot_token stores the workspace-specific xoxb-... token.
    For WhatsApp: external_workspace_id stores the WhatsApp phone number ID.
    """

    __tablename__ = "bot_platform_installations"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "external_workspace_id",
            name="uq_bot_installation_provider_workspace",
        ),
        Index("ix_bot_installation_provider", "provider"),
        Index("ix_bot_installation_org", "organisation_id"),
        Index("ix_bot_installation_workspace", "external_workspace_id"),
        Index("ix_bot_installation_active", "is_active"),
        {"schema": "public"},
    )

    installation_id: UUID = Field(
        default_factory=uuid4,
        primary_key=True,
        description="Unique ID for this installation.",
    )

    # Provider
    provider: BotPlatformProvider = Field(
        sa_column=Column(String(32), nullable=False),
        description="Bot platform (slack, teams, whatsapp, google_chat).",
    )

    # External workspace identification
    external_workspace_id: str = Field(
        sa_column=Column(String(512), nullable=False),
        description="Workspace/tenant ID from the platform (Slack team_id, Azure tenant_id).",
    )
    external_workspace_name: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Human-readable workspace/tenant name.",
    )

    # Credentials
    bot_token: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="Bot token for this workspace (e.g. Slack xoxb-...). NULL for Teams.",
    )
    bot_user_id: str | None = Field(
        default=None,
        sa_column=Column(String(255), nullable=True),
        description="Bot's user ID in this workspace (Slack bot_user_id).",
    )

    # Extra platform-specific data (scopes, service_url for Teams, etc.)
    extra_data: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column(JSON, nullable=True),
        description="Platform-specific metadata (scopes, service_url, team_url, etc.).",
    )

    # Organisation linkage (nullable — linked after first user authenticates)
    organisation_id: UUID | None = Field(
        default=None,
        sa_column=Column(
            ForeignKey("public.organisations.organisation_id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        description="CopilotGTM organisation this workspace is linked to. NULL until first user links.",
    )

    # Who installed
    installed_by_external_user_id: str | None = Field(
        default=None,
        sa_column=Column(String(512), nullable=True),
        description="External user ID of the person who installed the bot.",
    )

    # Status
    is_active: bool = Field(
        default=True,
        sa_column=Column(Boolean, nullable=False, server_default="true"),
        description="Whether this installation is active. Set to false on uninstall.",
    )
    installed_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False),
        description="When the bot was installed in this workspace.",
    )
    last_used_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
        description="Last time an event was received from this workspace.",
    )

    # Relationships
    organisation: Optional["Organisation"] = Relationship(
        back_populates="bot_platform_installations"
    )
