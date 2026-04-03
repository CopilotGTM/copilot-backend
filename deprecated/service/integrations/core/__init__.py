"""Core base classes and result models for integration services."""

from .base_sync_service import BaseSyncService, SyncResult

__all__ = [
    "BaseSyncService",
    "SyncResult",
]
