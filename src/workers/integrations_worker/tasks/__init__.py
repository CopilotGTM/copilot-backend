"""
Tasks for the integrations worker.

This module exports all integration-related Celery tasks.
"""

# Calendar sync tasks (three-tier architecture)
from .calendar import (
    sync_calendar_all_orgs_task,
    sync_calendar_integration_task,
    sync_calendar_org_task,
)

# CRM schema + record sync tasks (HubSpot)
from .crm import (
    mark_hubspot_initial_sync_failed_task,
    mark_hubspot_initial_sync_succeeded_task,
    mark_salesforce_initial_sync_failed_task,
    mark_salesforce_initial_sync_succeeded_task,
    process_hubspot_webhook_event_task,
    sync_hubspot_deal_schema_all_orgs_task,
    sync_hubspot_deal_schema_task,
    sync_hubspot_notes_task,
    sync_hubspot_pipelines_task,
    sync_hubspot_tasks_task,
    sync_hubspot_users_task,
    sync_salesforce_companies_task,
    sync_salesforce_contacts_task,
    sync_salesforce_deals_task,
    sync_salesforce_notes_task,
    sync_salesforce_opportunity_schema_all_orgs_task,
    sync_salesforce_opportunity_schema_task,
    sync_salesforce_pipelines_task,
    sync_salesforce_tasks_task,
    sync_salesforce_users_task,
)

# Email sync tasks (three-tier architecture)
from .email import (
    sync_email_all_orgs_task,
    sync_email_integration_task,
    sync_email_org_task,
)

# Messaging channels sync tasks (three-tier architecture)
from .messaging import (
    sync_deal_messages_all_orgs_task,
    sync_deal_messages_integration_task,
    sync_deal_messages_org_task,
    sync_messaging_channels_all_orgs_task,
    sync_messaging_channels_integration_task,
    sync_messaging_channels_org_task,
)

# Token refresh tasks (three-tier architecture)
from .token_refresh import (
    refresh_token_task,
    refresh_tokens_org_task,
    refresh_tokens_task,
)

__all__ = [
    # Token refresh (three-tier architecture)
    "refresh_tokens_task",
    "refresh_tokens_org_task",
    "refresh_token_task",
    # Calendar sync (three-tier architecture)
    "sync_calendar_all_orgs_task",
    "sync_calendar_org_task",
    "sync_calendar_integration_task",
    # Messaging channels sync (three-tier architecture)
    "sync_messaging_channels_all_orgs_task",
    "sync_messaging_channels_org_task",
    "sync_messaging_channels_integration_task",
    # Deal messages sync (three-tier architecture)
    "sync_deal_messages_all_orgs_task",
    "sync_deal_messages_org_task",
    "sync_deal_messages_integration_task",
    # Email sync (three-tier architecture)
    "sync_email_all_orgs_task",
    "sync_email_org_task",
    "sync_email_integration_task",
    # CRM schema sync (HubSpot)
    "sync_hubspot_deal_schema_all_orgs_task",
    "sync_hubspot_deal_schema_task",
    # CRM record sync (HubSpot, phase 2: pipelines + chain terminators)
    "sync_hubspot_pipelines_task",
    # CRM record sync (HubSpot, phase 3: users / owners)
    "sync_hubspot_users_task",
    "sync_hubspot_tasks_task",
    "sync_hubspot_notes_task",
    "process_hubspot_webhook_event_task",
    "sync_salesforce_companies_task",
    "sync_salesforce_contacts_task",
    "sync_salesforce_deals_task",
    "sync_salesforce_pipelines_task",
    "sync_salesforce_tasks_task",
    "sync_salesforce_notes_task",
    "sync_salesforce_users_task",
    "mark_hubspot_initial_sync_succeeded_task",
    "mark_hubspot_initial_sync_failed_task",
    "sync_salesforce_opportunity_schema_task",
    "sync_salesforce_opportunity_schema_all_orgs_task",
    "mark_salesforce_initial_sync_succeeded_task",
    "mark_salesforce_initial_sync_failed_task",
]
