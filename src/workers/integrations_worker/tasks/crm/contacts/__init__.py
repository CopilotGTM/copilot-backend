"""
CRM Contacts sync tasks following the three-tier architecture.

This module provides:
- sync_crm_contacts_all_orgs_task (Tier 1): Global coordinator, triggered by Celery Beat
- sync_crm_contacts_org_task (Tier 2): Organization-level processor
- sync_crm_contacts_integration_task (Tier 3): Manual trigger for single integration
"""

from .all_orgs_task import sync_crm_contacts_all_orgs_task
from .integration_task import sync_crm_contacts_integration_task
from .org_task import sync_crm_contacts_org_task

__all__ = [
    "sync_crm_contacts_all_orgs_task",
    "sync_crm_contacts_org_task",
    "sync_crm_contacts_integration_task",
]
