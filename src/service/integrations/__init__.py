"""
Integration services for managing third-party OAuth connections and data synchronization.

This module provides services for:
- OAuth flow management via Nango
- Token refresh and expiration handling
- Integration CRUD operations
- Data synchronization from external platforms
"""

from .calendar_sync_service import CalendarSyncService
from .integration_service import IntegrationService
from .nango_client import NangoClient
from .nango_webhook_service import NangoWebhookService, webhook_service
from .token_refresh_service import TokenRefreshService

# Note: DealChannelMessagesService is intentionally NOT exported here to avoid
# circular imports. The service imports from workers/providers, and workers/tasks
# imports back from service. Import directly from the module if needed:
# from src.service.integrations.deal_channel_messages_service import DealChannelMessagesService

__all__ = [
    "NangoClient",
    "IntegrationService",
    "TokenRefreshService",
    "NangoWebhookService",
    "webhook_service",
    "CalendarSyncService",
]
