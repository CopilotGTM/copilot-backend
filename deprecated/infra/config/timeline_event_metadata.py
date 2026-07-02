"""Deprecated timeline event source metadata.

Provides lightweight display metadata (e.g., icon/logo URL) for timeline event sources.
This keeps the frontend simple and consistent across surfaces that render timeline events.

If we later want to localize labels or support theming, this is the single registry.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.domain.models.organisation_tables.sync.timeline_models import TimelineEventSource


@dataclass(frozen=True)
class TimelineEventSourceMetadata:
    """Immutable display metadata for a timeline event source."""

    name: str
    logo_url: str | None


TIMELINE_EVENT_SOURCE_METADATA: dict[TimelineEventSource, TimelineEventSourceMetadata] = {
    TimelineEventSource.EMAIL_GMAIL: TimelineEventSourceMetadata(
        name="Gmail",
        logo_url="https://app.nango.dev/images/template-logos/google-mail.svg",
    ),
    TimelineEventSource.EMAIL_OUTLOOK: TimelineEventSourceMetadata(
        name="Outlook (Email)",
        logo_url="https://app.nango.dev/images/template-logos/outlook.svg",
    ),
    TimelineEventSource.CALENDAR_GOOGLE: TimelineEventSourceMetadata(
        name="Google Calendar",
        logo_url="https://api.nango.dev/images/template-logos/google-calendar.svg",
    ),
    TimelineEventSource.CALENDAR_OUTLOOK: TimelineEventSourceMetadata(
        name="Outlook Calendar",
        logo_url="https://app.nango.dev/images/template-logos/outlook.svg",
    ),
    TimelineEventSource.SLACK: TimelineEventSourceMetadata(
        name="Slack",
        logo_url="https://api.nango.dev/images/template-logos/slack.svg",
    ),
    TimelineEventSource.TEAMS: TimelineEventSourceMetadata(
        name="Microsoft Teams",
        logo_url="https://api.nango.dev/images/template-logos/microsoft-teams.svg",
    ),
    TimelineEventSource.CRM_HUBSPOT: TimelineEventSourceMetadata(
        name="HubSpot",
        logo_url="https://api.nango.dev/images/template-logos/hubspot.svg",
    ),
    TimelineEventSource.CRM_SALESFORCE: TimelineEventSourceMetadata(
        name="Salesforce",
        logo_url="https://api.nango.dev/images/template-logos/salesforce.svg",
    ),
    TimelineEventSource.SYSTEM: TimelineEventSourceMetadata(
        name="System",
        logo_url=None,
    ),
}


def get_timeline_event_source_metadata(
    source: TimelineEventSource | str | None,
) -> TimelineEventSourceMetadata | None:
    """Get metadata for a timeline source.

    Accepts either enum or raw string values; returns None if unknown.
    """

    if source is None:
        return None

    if isinstance(source, TimelineEventSource):
        return TIMELINE_EVENT_SOURCE_METADATA.get(source)

    try:
        return TIMELINE_EVENT_SOURCE_METADATA.get(TimelineEventSource(str(source)))
    except Exception:
        return None
