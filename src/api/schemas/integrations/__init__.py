"""
Integration schemas package.
"""

from .bot_integration_schemas import (
    AvailableBotIntegration,
    AvailableBotIntegrationsResponse,
    BotInstallationResponse,
    SlackBotConnectResponse,
    WhatsAppBotConnectResponse,
)
from .integration_schemas import (
    AvailableIntegration,
    AvailableIntegrationsResponse,
    # Channels
    ChannelItem,
    ChannelLinkRequest,
    ChannelLinkResponse,
    ChannelListResponse,
    EmptyResponse,
    # Available integrations
    IntegrationAdminPrerequisite,
    # Integration CRUD
    IntegrationCreate,
    # Errors
    IntegrationError,
    IntegrationListResponse,
    IntegrationResponse,
    IntegrationStatusResponse,
    IntegrationUpdate,
    LinkedChannel,
    OAuthCallbackRequest,
    # OAuth
    OAuthInitiateRequest,
    OAuthInitiateResponse,
    # Refresh
    RefreshChannelsResponse,
    SyncHistoryListResponse,
    SyncHistoryResponse,
    # Sync
    SyncTriggerRequest,
    SyncTriggerResponse,
)

__all__ = [
    # OAuth
    "OAuthInitiateRequest",
    "OAuthInitiateResponse",
    "OAuthCallbackRequest",
    # Integration CRUD
    "IntegrationCreate",
    "IntegrationUpdate",
    "IntegrationResponse",
    "IntegrationListResponse",
    "IntegrationStatusResponse",
    # Sync
    "SyncTriggerRequest",
    "SyncTriggerResponse",
    "SyncHistoryResponse",
    "SyncHistoryListResponse",
    # Refresh
    "RefreshChannelsResponse",
    # Available integrations
    "IntegrationAdminPrerequisite",
    "AvailableIntegration",
    "AvailableIntegrationsResponse",
    "AvailableBotIntegration",
    "AvailableBotIntegrationsResponse",
    "BotInstallationResponse",
    "SlackBotConnectResponse",
    "WhatsAppBotConnectResponse",
    # Errors
    "IntegrationError",
]
