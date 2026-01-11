"""
Email sync tasks following three-tier architecture.

Tier 1: sync_email_all_orgs_task - Triggered by Celery Beat, syncs all orgs
Tier 2: sync_email_org_task - Syncs all email integrations for one org
Tier 3: sync_email_integration_task - Syncs specific integration (manual trigger)

Usage:
    # Scheduled sync (Celery Beat triggers Tier 1)
    # Tier 1 dispatches Tier 2 for each org
    # Tier 2 calls EmailDeltaSyncService for each integration

    # Manual sync (frontend refresh button triggers Tier 3)
    sync_email_integration_task.apply_async(
        kwargs={"organisation_id": "...", "integration_id": "..."}
    )
"""

from .all_orgs_task import sync_email_all_orgs_task
from .integration_task import sync_email_integration_task
from .org_task import sync_email_org_task

__all__ = [
    "sync_email_all_orgs_task",
    "sync_email_org_task",
    "sync_email_integration_task",
]
