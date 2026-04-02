"""
CRM Deals sync tasks following the three-tier architecture.

This module provides:
- sync_crm_deals_all_orgs_task (Tier 1): Global coordinator, triggered by Celery Beat
- sync_crm_deals_org_task (Tier 2): Organization-level processor
- sync_crm_deals_integration_task (Tier 3): Manual trigger for single integration

Architecture:
    Celery Beat (every 1 hour)
        |
    sync_crm_deals_all_orgs_task [Tier 1]
        |
    sync_crm_deals_org_task [Tier 2] (one per org, async dispatch)
        |
    DealSyncService (for each integration, inline)

Usage:
    # Scheduled sync (via Celery Beat config)
    "task": "integrations.sync_crm_deals_all_orgs"

    # Manual org sync
    sync_crm_deals_org_task.delay(organisation_id="uuid")

    # Manual integration sync
    sync_crm_deals_integration_task.delay(
        organisation_id="uuid",
        integration_id="uuid"
    )
"""

from .all_orgs_task import sync_crm_deals_all_orgs_task
from .integration_task import sync_crm_deals_integration_task
from .org_task import sync_crm_deals_org_task

__all__ = [
    "sync_crm_deals_all_orgs_task",
    "sync_crm_deals_org_task",
    "sync_crm_deals_integration_task",
]
