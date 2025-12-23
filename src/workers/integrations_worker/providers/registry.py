"""Simple provider registry for sync dispatch.

Maps (category, provider) -> fetch callable

Fetch signature:
    async fetch(nango, connection_id: str, is_initial: bool, last_synced_at, data_type: str | None, **kw) -> list[dict]
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from .calendar.google_calendar import fetch_events as google_calendar_fetch
from .calendar.outlook_calendar import fetch_events as outlook_calendar_fetch
from .conversation_intelligence.gong import (
    fetch_analytics as gong_analytics_fetch,
)
from .conversation_intelligence.gong import (
    fetch_calls as gong_calls_fetch,
)
from .conversation_intelligence.gong import (
    fetch_speakers as gong_speakers_fetch,
)
from .conversation_intelligence.gong import (
    fetch_transcript_segments as gong_segments_fetch,
)
from .crm.hubspot import (
    fetch_companies as hubspot_companies_fetch,
)
from .crm.hubspot import (
    fetch_contacts as hubspot_contacts_fetch,
)
from .crm.hubspot import (
    fetch_deal_tasks as hubspot_deal_tasks_fetch,
)
from .crm.hubspot import (
    fetch_deals as hubspot_deals_fetch,
)
from .crm.hubspot import (
    fetch_notes as hubspot_notes_fetch,
)
from .crm.hubspot import (
    fetch_owners as hubspot_owners_fetch,
)
from .crm.hubspot import (
    fetch_pipelines as hubspot_pipelines_fetch,
)
from .crm.hubspot import (
    fetch_users as hubspot_users_fetch,
)
from .crm.salesforce import (
    fetch_accounts as salesforce_accounts_fetch,
)
from .crm.salesforce import (
    fetch_contacts as salesforce_contacts_fetch,
)
from .crm.salesforce import (
    fetch_deals as salesforce_deals_fetch,
)
from .crm.salesforce import (
    fetch_leads as salesforce_leads_fetch,
)
from .crm.salesforce import (
    fetch_notes as salesforce_notes_fetch,
)
from .crm.salesforce import (
    fetch_pipelines as salesforce_pipelines_fetch,
)
from .crm.salesforce import (
    fetch_tasks as salesforce_tasks_fetch,
)
from .crm.salesforce import (
    fetch_tickets as salesforce_tickets_fetch,
)
from .crm.salesforce import (
    fetch_users as salesforce_users_fetch,
)
from .email.outlook_email import fetch_folders as outlook_email_fetch_folders
from .email.outlook_email import fetch_messages as outlook_email_fetch_messages
from .messaging.teams import fetch_channels as teams_fetch_channels
from .messaging.teams import fetch_chat_messages as teams_fetch_chat_messages
from .messaging.teams import fetch_chats as teams_fetch_chats
from .messaging.teams import fetch_messages as teams_fetch_messages

FetchFn = Callable[..., Awaitable[list[dict]]]

_REGISTRY: dict[tuple[str, str], dict[str, FetchFn]] = {}


def _init_defaults() -> None:
    # Calendar: Google
    _REGISTRY[("calendar", "google")] = {
        "events": google_calendar_fetch,
        "calendars": google_calendar_fetch,
    }
    # Calendar: Outlook
    _REGISTRY[("calendar", "outlook")] = {
        "events": outlook_calendar_fetch,
        "calendars": outlook_calendar_fetch,
    }
    # CRM: HubSpot
    _REGISTRY[("crm", "hubspot")] = {
        "pipelines": hubspot_pipelines_fetch,
        "deals": hubspot_deals_fetch,
        "deal_tasks": hubspot_deal_tasks_fetch,
        "notes": hubspot_notes_fetch,
        "contacts": hubspot_contacts_fetch,
        "users": hubspot_users_fetch,
        "companies": hubspot_companies_fetch,
        "owners": hubspot_owners_fetch,
    }
    # CRM: Salesforce (Phase 1 scaffold)
    _REGISTRY[("crm", "salesforce")] = {
        "pipelines": salesforce_pipelines_fetch,
        "deals": salesforce_deals_fetch,
        "opportunities": salesforce_deals_fetch,  # Alias
        "deal_tasks": salesforce_tasks_fetch,
        "notes": salesforce_notes_fetch,
        "tickets": salesforce_tickets_fetch,  # Alias to Salesforce Task (Activities)
        "contacts": salesforce_contacts_fetch,
        "users": salesforce_users_fetch,
        "owners": salesforce_users_fetch,  # Alias
        "accounts": salesforce_accounts_fetch,
        "companies": salesforce_accounts_fetch,  # Alias
        "leads": salesforce_leads_fetch,
    }
    # Conversation Intelligence: Gong
    _REGISTRY[("conversation_intelligence", "gong")] = {
        "calls": gong_calls_fetch,
        "call_speakers": gong_speakers_fetch,
        "speakers": gong_speakers_fetch,
        "transcript_segments": gong_segments_fetch,
        "call_transcript_segments": gong_segments_fetch,
        "call_analytics": gong_analytics_fetch,
        "analytics": gong_analytics_fetch,
    }
    # Email: Outlook
    _REGISTRY[("email", "outlook")] = {
        "folders": outlook_email_fetch_folders,
        "messages": outlook_email_fetch_messages,
        "emails": outlook_email_fetch_messages,  # Alias
    }
    # Messaging: Teams
    _REGISTRY[("messaging", "teams")] = {
        "channels": teams_fetch_channels,
        "chats": teams_fetch_chats,  # 1:1, group, meeting chats
        "messages": teams_fetch_messages,  # Channel messages
        "chat_messages": teams_fetch_chat_messages,  # Chat messages
    }


def get_fetcher(category: str, provider: str, data_type: str) -> FetchFn | None:
    if not _REGISTRY:
        _init_defaults()
    entry = _REGISTRY.get((category, provider))
    if not entry:
        return None

    # Try direct lookup first
    fetcher = entry.get(data_type)
    if fetcher:
        return fetcher

    # Handle aliases for backward compatibility
    # Map 'tasks' to 'deal_tasks' for CRM providers
    if category == "crm" and data_type == "tasks":
        return entry.get("deal_tasks")

    return None


__all__ = ["get_fetcher"]
