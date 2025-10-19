"""Simple provider registry for sync dispatch.

Maps (category, provider) -> fetch callable

Fetch signature:
    async fetch(nango, connection_id: str, is_initial: bool, last_synced_at, data_type: str | None, **kw) -> list[dict]
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from .calendar.google_calendar import fetch_events as google_calendar_fetch

FetchFn = Callable[..., Awaitable[list[dict]]]

_REGISTRY: dict[tuple[str, str], dict[str, FetchFn]] = {}


def _init_defaults() -> None:
    # Calendar: Google
    _REGISTRY[("calendar", "google")] = {"events": google_calendar_fetch}


def get_fetcher(category: str, provider: str, data_type: str) -> FetchFn | None:
    if not _REGISTRY:
        _init_defaults()
    entry = _REGISTRY.get((category, provider))
    if not entry:
        return None
    return entry.get(data_type)


__all__ = ["get_fetcher"]
