"""
Integration schemas package.
"""

from .integration_schemas import (
    # Available integrations
    AvailableIntegration,
    AvailableIntegrationsResponse,
    # Channels
    ChannelItem,
    ChannelLinkRequest,
    ChannelLinkResponse,
    ChannelListResponse,
    EmptyResponse,
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
    "AvailableIntegration",
    "AvailableIntegrationsResponse",
    # Errors
    "IntegrationError",
]
