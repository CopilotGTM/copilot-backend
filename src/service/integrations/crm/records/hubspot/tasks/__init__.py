"""HubSpot CRM task record-sync package."""

from src.service.integrations.crm.records.hubspot.tasks.task_sync_service import (
    HubSpotTaskRecordSyncService,
    TaskSyncConfigurationError,
)

__all__ = [
    "HubSpotTaskRecordSyncService",
    "TaskSyncConfigurationError",
]
