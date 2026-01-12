"""
Calendar sync tasks following the three-tier architecture.

This module provides:
- sync_calendar_all_orgs_task (Tier 1): Global coordinator, triggered by Celery Beat
- sync_calendar_org_task (Tier 2): Organization-level processor
- sync_calendar_integration_task (Tier 3): Manual trigger for single integration

Architecture:
    Celery Beat (every 30 min)
        |
    sync_calendar_all_orgs_task [Tier 1]
        |
    sync_calendar_org_task [Tier 2] (one per org, async dispatch)
        |
    CalendarSyncService (direct call, for each integration)

Usage:
    # Scheduled sync (via Celery Beat config)
    "task": "integrations.sync_calendar_all_orgs"

    # Manual org sync
    sync_calendar_org_task.delay(organisation_id="uuid")

    # Manual integration sync
    sync_calendar_integration_task.delay(
        organisation_id="uuid",
        integration_id="uuid"
    )
"""

from .all_orgs_task import sync_calendar_all_orgs_task
from .integration_task import sync_calendar_integration_task
from .org_task import sync_calendar_org_task

__all__ = [
    "sync_calendar_all_orgs_task",
    "sync_calendar_org_task",
    "sync_calendar_integration_task",
]
