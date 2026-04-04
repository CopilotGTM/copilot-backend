"""
Integration services for managing OAuth connections and data synchronization.

This module provides services for:
- Custom OAuth flow management (Google, Microsoft)
- Token refresh and expiration handling via token_resolution
- Integration CRUD operations
- Data synchronization from external platforms
"""

from .integration_service import IntegrationService

__all__ = [
    "IntegrationService",
]
