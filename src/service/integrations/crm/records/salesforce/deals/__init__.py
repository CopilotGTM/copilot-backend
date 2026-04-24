"""Salesforce opportunity/deal fact sync."""

from src.service.integrations.crm.records.salesforce.deals.deal_sync_service import (
    SalesforceDealRecordSyncService,
    SalesforceDealSyncConfigurationError,
)

__all__ = [
    "SalesforceDealRecordSyncService",
    "SalesforceDealSyncConfigurationError",
]
