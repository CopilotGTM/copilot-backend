"""Tests for Microsoft OAuth scopes requested per integration type."""

from src.domain.models.organisation_tables.sync import IntegrationType
from src.infra.config.integrations_metadata import INTEGRATION_METADATA
from src.service.integrations.providers.microsoft_provider import MicrosoftProvider


def test_outlook_email_requests_only_profile_and_mail_scopes() -> None:
    assert MicrosoftProvider(IntegrationType.OUTLOOK_EMAIL)._get_scopes() == [
        "offline_access",
        "User.Read",
        "Mail.Read",
    ]


def test_outlook_calendar_requests_profile_calendar_and_meeting_scopes() -> None:
    assert MicrosoftProvider(IntegrationType.OUTLOOK_CALENDAR)._get_scopes() == [
        "offline_access",
        "User.Read",
        "Calendars.Read",
        "OnlineMeetings.Read",
    ]


def test_teams_requests_only_message_sync_scopes() -> None:
    assert MicrosoftProvider(IntegrationType.TEAMS)._get_scopes() == [
        "offline_access",
        "User.Read",
        "Team.ReadBasic.All",
        "Channel.ReadBasic.All",
        "Chat.Read",
    ]


def test_microsoft_integration_metadata_matches_provider_requested_scopes() -> None:
    for integration_type in (
        IntegrationType.OUTLOOK_EMAIL,
        IntegrationType.OUTLOOK_CALENDAR,
        IntegrationType.TEAMS,
    ):
        assert (
            INTEGRATION_METADATA[integration_type].oauth_scopes
            == MicrosoftProvider(integration_type)._get_scopes()
        )
