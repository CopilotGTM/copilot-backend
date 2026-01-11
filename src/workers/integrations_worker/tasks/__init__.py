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

# DEPRECATED: Use the three-tier tasks above instead
from .calendar_sync_tasks import sync_calendar_events_task
from .deal_channel_messages_tasks import (
    sync_deal_channel_messages_task,
    trigger_deal_channel_messages_sync_all_orgs,
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

# Core sync tasks
from .sync_tasks import sync_integration_data_task
from .teams_sync_tasks import sync_teams_channels_task

# Token refresh tasks
from .token_refresh import (
    refresh_expiring_tokens_task,
    refresh_single_integration_task,
)

__all__ = [
    # Token refresh
    "refresh_expiring_tokens_task",
    "refresh_single_integration_task",
    # Core sync
    "sync_integration_data_task",
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
    # DEPRECATED: Use three-tier tasks above
    "sync_calendar_events_task",  # DEPRECATED: use sync_calendar_all_orgs_task
    "sync_teams_channels_task",  # DEPRECATED: use sync_messaging_channels_all_orgs_task
    "sync_deal_channel_messages_task",  # DEPRECATED: use sync_deal_messages_org_task
    "trigger_deal_channel_messages_sync_all_orgs",  # DEPRECATED: use sync_deal_messages_all_orgs_task
]
