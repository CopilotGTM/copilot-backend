from types import SimpleNamespace

from src.domain.models.organisation_tables.sync.integration_models import IntegrationType
from src.domain.models.organisation_tables.sync.timeline_models import TimelineEventSource
from src.service.crm.timeline.timeline_event_source_resolver import (
    resolve_calendar_event_source,
    resolve_email_event_source,
    resolve_meeting_event_source,
)


def test_resolve_email_event_source_gmail() -> None:
    assert resolve_email_event_source(IntegrationType.GMAIL) == TimelineEventSource.EMAIL_GMAIL


def test_resolve_email_event_source_outlook() -> None:
    assert (
        resolve_email_event_source(IntegrationType.OUTLOOK_EMAIL)
        == TimelineEventSource.EMAIL_OUTLOOK
    )


def test_resolve_calendar_event_source_outlook() -> None:
    assert (
        resolve_calendar_event_source(IntegrationType.OUTLOOK_CALENDAR)
        == TimelineEventSource.CALENDAR_OUTLOOK
    )


def test_resolve_meeting_event_source_falls_back_to_calendar_provider() -> None:
    event = SimpleNamespace(online_meeting_provider=None, conference_link=None)
    assert (
        resolve_meeting_event_source(event, IntegrationType.GOOGLE_CALENDAR)
        == TimelineEventSource.CALENDAR_GOOGLE
    )


def test_resolve_meeting_event_source_does_not_override_with_teams() -> None:
    event = SimpleNamespace(
        online_meeting_provider="teams",
        conference_link="https://teams.microsoft.com/l/meetup-join/whatever",
    )
    assert (
        resolve_meeting_event_source(event, IntegrationType.OUTLOOK_CALENDAR)
        == TimelineEventSource.CALENDAR_OUTLOOK
    )
