"""Synced data domain models (calendar + future CRM, email, etc.)."""

from .base import SyncCategory, SyncedRecordMixin
from .calendar import CalendarAttendee, CalendarEvent
from .integration_models import (
    IntegrationScope,
    IntegrationSyncHistory,
    IntegrationType,
    KnowledgeIntegration,
    SyncStatus,
)

__all__ = [
    "SyncCategory",
    "SyncedRecordMixin",
    "CalendarEvent",
    "CalendarAttendee",
    "IntegrationType",
    "IntegrationScope",
    "KnowledgeIntegration",
    "IntegrationSyncHistory",
    "SyncStatus",
]
