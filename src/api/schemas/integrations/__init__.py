"""
Integration schemas package.
"""

from .integration_schemas import (
    # OAuth
    OAuthInitiateRequest,
    OAuthInitiateResponse,
    OAuthCallbackRequest,
    # Integration CRUD
    IntegrationCreate,
    IntegrationUpdate,
    IntegrationResponse,
    IntegrationListResponse,
    IntegrationStatusResponse,
    # Sync
    SyncTriggerRequest,
    SyncTriggerResponse,
    SyncHistoryResponse,
    SyncHistoryListResponse,
    # Available integrations
    AvailableIntegration,
    AvailableIntegrationsResponse,
    # Errors
    IntegrationError,
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
    # Available integrations
    "AvailableIntegration",
    "AvailableIntegrationsResponse",
    # Errors
    "IntegrationError",
]
