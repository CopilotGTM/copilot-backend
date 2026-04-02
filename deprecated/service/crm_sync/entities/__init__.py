"""CRM entity sync services."""

# Backward compatibility: Enrichment services moved to enrichment/ module
# These re-exports ensure existing imports continue to work
from src.service.integrations.crm.enrichment import (
    DealEnrichmentService,
    OwnerEnrichmentService,
    TaskEnrichmentService,
)

from .company_sync_service import CompanySyncService
from .contact_sync_service import ContactSyncService
from .deal_sync_service import DealSyncService
from .deal_task_sync_service import DealTaskSyncService
from .owner_sync_service import OwnerSyncService
from .pipeline_sync_service import PipelineSyncService
from .user_sync_service import UserSyncService

__all__ = [
    "CompanySyncService",
    "ContactSyncService",
    "DealEnrichmentService",
    "DealSyncService",
    "DealTaskSyncService",
    "OwnerEnrichmentService",
    "OwnerSyncService",
    "PipelineSyncService",
    "TaskEnrichmentService",
    "UserSyncService",
]
