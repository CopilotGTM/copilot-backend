"""
Tasks for the integrations worker.

This module exports all integration-related Celery tasks.
CRM sync tasks (HubSpot, Salesforce) deprecated — moved to deprecated/workers/.
"""

# Calendar sync tasks (three-tier architecture)
from .calendar import (
    sync_calendar_all_orgs_task,
    sync_calendar_integration_task,
    sync_calendar_org_task,
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
]
