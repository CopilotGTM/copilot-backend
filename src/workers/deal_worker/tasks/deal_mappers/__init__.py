"""Deal Mappers - Tasks for mapping calendar, email, and stakeholders to deals.

This module contains the task architecture for deal mapping:

Task 1: Stakeholder Sync (stakeholder_tasks.py)
    - sync_deal_stakeholders_task: Per-deal stakeholder sync from CRM participants
    - sync_all_deals_stakeholders_org_task: Org-level sync for ALL deals (initial sync)
    - Triggers Task 2 & 3 when stakeholders are synced

Task 2: Calendar Matching (calendar_matching_tasks.py)
    - Matches calendar events to deals based on team + contact participation
    - Triggered by calendar sync or stakeholder addition

Task 3: Email Matching (email_matching_tasks.py)
    - Matches emails to deals based on team + contact participation
    - Triggered by email sync or stakeholder addition

Task 4: Timeline Sync (timeline_sync_tasks.py)
    - Syncs timeline events (EMAIL, MEETING, STAGE_CHANGE, DEAL_CREATED)
    - Used for backfilling existing data

Architecture:
    CRM Initial Sync → enrich_crm_after_initial_sync_task
                            │
                            └──→ sync_all_deals_stakeholders_org_task (org-level)
                                        │
                                        └──→ Task 2 (Calendar) + Task 3 (Email)

    CRM Incremental Sync → sync_deal_stakeholders_task (per-deal)
                                │
                                └──→ Task 2 (Calendar) + Task 3 (Email)

    Calendar Sync → Task 2 (org-wide matching)
    Email Sync → Task 3 (org-wide matching)
    Timeline Backfill → Task 4 (timeline sync)
"""

from src.workers.deal_worker.tasks.deal_mappers.calendar_contact_account_discovery_tasks import (
    discover_calendar_contacts_accounts_task,
)
from src.workers.deal_worker.tasks.deal_mappers.calendar_matching_tasks import (
    match_calendar_to_deals_all_orgs_task,
    match_calendar_to_deals_org_task,
    match_calendar_to_deals_task,
)
from src.workers.deal_worker.tasks.deal_mappers.email_matching_tasks import (
    match_email_to_deals_org_task,
    match_email_to_deals_task,
)
from src.workers.deal_worker.tasks.deal_mappers.stakeholder_tasks import (
    sync_all_deals_stakeholders_org_task,
    sync_deal_stakeholders_task,
)
from src.workers.deal_worker.tasks.deal_mappers.timeline_sync_tasks import (
    exclude_timeline_event_task,
    sync_deal_timeline_task,
    sync_org_timeline_task,
    trigger_timeline_sync_all_orgs_task,
)

__all__ = [
    # Task 1: Stakeholder Sync
    "sync_deal_stakeholders_task",
    "sync_all_deals_stakeholders_org_task",
    # Task 2: Calendar Matching
    "discover_calendar_contacts_accounts_task",
    "match_calendar_to_deals_all_orgs_task",
    "match_calendar_to_deals_org_task",
    "match_calendar_to_deals_task",
    # Task 3: Email Matching
    "match_email_to_deals_org_task",
    "match_email_to_deals_task",
    # Task 4: Timeline Sync
    "trigger_timeline_sync_all_orgs_task",
    "sync_org_timeline_task",
    "sync_deal_timeline_task",
    "exclude_timeline_event_task",
]
