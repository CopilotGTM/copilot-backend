"""
CRM Pipelines sync tasks following the three-tier architecture.

This module provides:
- sync_crm_pipelines_all_orgs_task (Tier 1): Global coordinator, triggered by Celery Beat
- sync_crm_pipelines_org_task (Tier 2): Organization-level processor
- sync_crm_pipelines_integration_task (Tier 3): Manual trigger for single integration

Architecture:
    Celery Beat (every 3 hours)
        |
    sync_crm_pipelines_all_orgs_task [Tier 1]
        |
    sync_crm_pipelines_org_task [Tier 2] (one per org, async dispatch)
        |
    PipelineSyncService (for each integration, inline)
"""

from .all_orgs_task import sync_crm_pipelines_all_orgs_task
from .integration_task import sync_crm_pipelines_integration_task
from .org_task import sync_crm_pipelines_org_task

__all__ = [
    "sync_crm_pipelines_all_orgs_task",
    "sync_crm_pipelines_org_task",
    "sync_crm_pipelines_integration_task",
]
