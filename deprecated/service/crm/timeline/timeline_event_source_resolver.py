"""Deprecated timeline event source resolution.

Historically, timeline creation defaulted to Google/Gmail sources. That caused
provider mis-labeling (e.g., Outlook meetings shown as Google).

This module centralizes best-effort inference of TimelineEventSource from either:
- the owning integration type (preferred), and/or
- calendar meeting metadata (e.g., Teams online meeting signals).
"""

from __future__ import annotations

from typing import Any

from src.domain.models.organisation_tables.sync.integration_models import IntegrationType
from src.domain.models.organisation_tables.sync.timeline_models import TimelineEventSource


def resolve_email_event_source(
    integration_type: IntegrationType | str | None,
) -> TimelineEventSource | None:
    if integration_type is None:
        return None

    it = (
        integration_type
        if isinstance(integration_type, IntegrationType)
        else IntegrationType(str(integration_type))
    )
    if it == IntegrationType.GMAIL:
        return TimelineEventSource.EMAIL_GMAIL
    if it == IntegrationType.OUTLOOK_EMAIL:
        return TimelineEventSource.EMAIL_OUTLOOK
    return None


def resolve_calendar_event_source(
    integration_type: IntegrationType | str | None,
) -> TimelineEventSource | None:
    if integration_type is None:
        return None

    it = (
        integration_type
        if isinstance(integration_type, IntegrationType)
        else IntegrationType(str(integration_type))
    )
    if it == IntegrationType.GOOGLE_CALENDAR:
        return TimelineEventSource.CALENDAR_GOOGLE
    if it == IntegrationType.OUTLOOK_CALENDAR:
        return TimelineEventSource.CALENDAR_OUTLOOK
    return None


def _looks_like_teams_meeting(event: Any) -> bool:
    """Detect Microsoft Teams meetings from CalendarEvent-like objects."""

    provider = (getattr(event, "online_meeting_provider", None) or "").strip().lower()
    if provider:
        if "teams" in provider:
            return True
        if "microsoft" in provider and "team" in provider:
            return True

    conference_link = (getattr(event, "conference_link", None) or "").strip().lower()
    if conference_link:
        if "teams.microsoft.com" in conference_link:
            return True
        if "teams.live.com" in conference_link:
            return True

    # Some payloads may omit provider but include a stable online meeting id.
    online_meeting_id = getattr(event, "online_meeting_id", None)
    return bool(online_meeting_id and provider == "teams")


def resolve_meeting_event_source(
    event: Any | None,
    calendar_integration_type: IntegrationType | str | None,
) -> TimelineEventSource | None:
    """Resolve source for a meeting timeline event.

    For now, `TimelineEventSource` for meetings represents the *calendar provider*
    (Google Calendar vs Outlook Calendar), derived from the owning integration.

    Conference provider (Teams/Meet/Zoom) should be captured separately (e.g. in
    `event_metadata`) as a secondary attribute; we intentionally do not override
    the calendar provider here.
    """

    _ = event  # reserved for future conference-provider inference
    return resolve_calendar_event_source(calendar_integration_type)


def resolve_crm_event_source(
    integration_type: IntegrationType | str | None,
) -> TimelineEventSource:
    """Resolve source for CRM timeline events (stage change, deal created).

    Returns:
        TimelineEventSource.CRM_HUBSPOT or CRM_SALESFORCE based on integration type.
        Defaults to CRM_HUBSPOT if integration type is unknown or None.
    """
    if integration_type is None:
        return TimelineEventSource.CRM_HUBSPOT

    it = (
        integration_type
        if isinstance(integration_type, IntegrationType)
        else IntegrationType(str(integration_type))
    )
    if it == IntegrationType.SALESFORCE:
        return TimelineEventSource.CRM_SALESFORCE
    if it == IntegrationType.HUBSPOT:
        return TimelineEventSource.CRM_HUBSPOT

    return TimelineEventSource.CRM_HUBSPOT
