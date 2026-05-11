"""Schemas for user-facing bot integration management."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

BotProvider = Literal["slack", "whatsapp"]
BotConnectionMode = Literal["oauth_install", "qr_deep_link"]
BotAvailabilityStatus = Literal["available", "coming_soon", "beta", "deprecated"]
BotActionRequired = Literal[
    "none",
    "install_required",
    "identity_link_required",
    "setup_required",
]


class BotInstallationResponse(BaseModel):
    """Workspace-level bot installation details safe to show in the app."""

    installation_id: UUID
    workspace_id: str
    workspace_name: str | None = None
    installed_at: datetime | None = None
    last_used_at: datetime | None = None


class AvailableBotIntegration(BaseModel):
    """A bot provider card for the Integrations > Bots section."""

    provider: BotProvider
    name: str
    display_name: str
    description: str
    logo_url: str | None = None
    category: Literal["bots"] = "bots"
    category_name: Literal["Bots"] = "Bots"
    availability_status: BotAvailabilityStatus = "available"
    connection_mode: BotConnectionMode = "oauth_install"
    connect_url: str | None = None
    installed: bool = False
    installation: BotInstallationResponse | None = None
    identity_linked: bool = False
    identity_mapping_id: UUID | None = None
    action_required: BotActionRequired = "install_required"


class AvailableBotIntegrationsResponse(BaseModel):
    """Response for listing available bot providers."""

    bots: list[AvailableBotIntegration] = Field(default_factory=list)
    total_available: int
    total_installed: int
    total_linked: int


class SlackBotConnectResponse(BaseModel):
    """Slack OAuth install URL for the CopilotGTM Slack bot."""

    provider: Literal["slack"] = "slack"
    connect_url: str
    expires_at: str | None = None


class WhatsAppBotConnectResponse(BaseModel):
    """WhatsApp click-to-chat URL for the CopilotGTM WhatsApp bot."""

    provider: Literal["whatsapp"] = "whatsapp"
    connect_url: str
    expires_at: str | None = None
