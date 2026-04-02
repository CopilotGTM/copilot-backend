"""Simple provider registry for sync dispatch.

Maps (category, provider) -> fetch callable

Fetch signature:
    async fetch(nango, connection_id: str, is_initial: bool, last_synced_at, data_type: str | None, **kw) -> list[dict]
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from .calendar.google_calendar import fetch_events as google_calendar_fetch
from .calendar.outlook_calendar import fetch_events as outlook_calendar_fetch

# CRM providers (HubSpot, Salesforce) deprecated — moved to deprecated/workers/
# Will be restored for one-time data migration in the future.
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
    # CRM providers (HubSpot, Salesforce) deprecated — sync removed, migration TBD
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

    return None


__all__ = ["get_fetcher"]
