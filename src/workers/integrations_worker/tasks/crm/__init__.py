"""
CRM sync tasks following the three-tier architecture.

This module provides scheduled sync tasks for CRM entities:
- Deals: sync_crm_deals_* tasks (hourly)
- Pipelines: sync_crm_pipelines_* tasks (every 3 hours)
- Contacts: sync_crm_contacts_* tasks (every 3 hours)
- Companies: sync_crm_companies_* tasks (every 3 hours)
- Users: sync_crm_users_* tasks (every 3 hours)

Architecture:
    Celery Beat (scheduled)
        |
    sync_crm_{entity}_all_orgs_task [Tier 1]
        |
    sync_crm_{entity}_org_task [Tier 2] (one per org, async dispatch)
        |
    Sync service (inline per integration)

Each entity follows the three-tier pattern:
- Tier 1: Global coordinator, triggered by Celery Beat
- Tier 2: Organization-level processor, syncs all integrations for one org
- Tier 3: Manual trigger for single integration

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

from .activities import (
    sync_crm_activities_all_orgs_task,
    sync_crm_activities_integration_task,
    sync_crm_activities_org_task,
)
from .companies import (
    sync_crm_companies_all_orgs_task,
    sync_crm_companies_integration_task,
    sync_crm_companies_org_task,
)
from .contacts import (
    sync_crm_contacts_all_orgs_task,
    sync_crm_contacts_integration_task,
    sync_crm_contacts_org_task,
)
from .deals import (
    sync_crm_deals_all_orgs_task,
    sync_crm_deals_integration_task,
    sync_crm_deals_org_task,
)
from .enrichment_task import enrich_crm_after_initial_sync_task
from .pipelines import (
    sync_crm_pipelines_all_orgs_task,
    sync_crm_pipelines_integration_task,
    sync_crm_pipelines_org_task,
)
from .users import (
    sync_crm_users_all_orgs_task,
    sync_crm_users_integration_task,
    sync_crm_users_org_task,
)

__all__ = [
    # Activities (tasks/notes)
    "sync_crm_activities_all_orgs_task",
    "sync_crm_activities_org_task",
    "sync_crm_activities_integration_task",
    # Deals
    "sync_crm_deals_all_orgs_task",
    "sync_crm_deals_org_task",
    "sync_crm_deals_integration_task",
    # Pipelines
    "sync_crm_pipelines_all_orgs_task",
    "sync_crm_pipelines_org_task",
    "sync_crm_pipelines_integration_task",
    # Contacts
    "sync_crm_contacts_all_orgs_task",
    "sync_crm_contacts_org_task",
    "sync_crm_contacts_integration_task",
    # Companies
    "sync_crm_companies_all_orgs_task",
    "sync_crm_companies_org_task",
    "sync_crm_companies_integration_task",
    # Users
    "sync_crm_users_all_orgs_task",
    "sync_crm_users_org_task",
    "sync_crm_users_integration_task",
    # Enrichment (for initial sync)
    "enrich_crm_after_initial_sync_task",
]
