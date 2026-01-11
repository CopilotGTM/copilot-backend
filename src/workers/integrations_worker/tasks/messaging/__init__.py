"""
Messaging sync tasks - Three-tier architecture.

This module provides Celery tasks for syncing messaging data:
- Teams channels and chats
- Deal-linked channel messages

Three-tier architecture for channels/chats:
- Tier 1: sync_messaging_channels_all_orgs_task - Global coordinator (Beat, automatic)
- Tier 2: sync_messaging_channels_org_task - Organization-level processor
- Tier 3: sync_messaging_channels_integration_task - Manual trigger (frontend)

Three-tier architecture for deal messages:
- Tier 1: sync_deal_messages_all_orgs_task - Global coordinator (Beat, automatic)
- Tier 2: sync_deal_messages_org_task - Organization-level processor
- Tier 3: sync_deal_messages_integration_task - Manual trigger (frontend)

Automatic sync flow (Celery Beat):
    Tier 1 → Tier 2 (one task per org)

Manual sync flow (Frontend refresh):
    Tier 3 (directly for specific integration)
"""

from .channels_all_orgs_task import sync_messaging_channels_all_orgs_task
from .channels_integration_task import sync_messaging_channels_integration_task
from .channels_org_task import sync_messaging_channels_org_task
from .deal_messages_all_orgs_task import sync_deal_messages_all_orgs_task
from .deal_messages_integration_task import sync_deal_messages_integration_task
from .deal_messages_org_task import sync_deal_messages_org_task

__all__ = [
    # Channels/Chats sync (three-tier)
    "sync_messaging_channels_all_orgs_task",
    "sync_messaging_channels_org_task",
    "sync_messaging_channels_integration_task",
    # Deal messages sync (three-tier)
    "sync_deal_messages_all_orgs_task",
    "sync_deal_messages_org_task",
    "sync_deal_messages_integration_task",
]
