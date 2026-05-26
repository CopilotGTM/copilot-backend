"""Tests for Microsoft OAuth scopes requested per integration type."""

from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest
from src.domain.models.organisation_tables.sync import IntegrationType
from src.infra.config.configs import microsoft_integration_config
from src.infra.config.integrations_metadata import INTEGRATION_METADATA
from src.service.integrations.providers.microsoft_provider import MicrosoftProvider


def test_outlook_email_requests_only_profile_and_mail_scopes() -> None:
    assert MicrosoftProvider(IntegrationType.OUTLOOK_EMAIL)._get_scopes() == [
        "offline_access",
        "User.Read",
        "Mail.Read",
        "Mail.Send",
    ]


def test_outlook_calendar_requests_profile_calendar_and_meeting_scopes() -> None:
    assert MicrosoftProvider(IntegrationType.OUTLOOK_CALENDAR)._get_scopes() == [
        "offline_access",
        "User.Read",
        "Calendars.ReadWrite",
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


@pytest.mark.asyncio
async def test_microsoft_authorization_url_does_not_force_user_consent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_get_credentials() -> tuple[str, str]:
        return ("client-id", "client-secret")

    monkeypatch.setattr(
        microsoft_integration_config,
        "REDIRECT_URI",
        "https://api.example.com/users/integrations/copilot-oauth/callback",
    )
    monkeypatch.setattr(
        microsoft_integration_config,
        "get_credentials",
        AsyncMock(side_effect=_fake_get_credentials),
    )

    provider = MicrosoftProvider(IntegrationType.TEAMS)
    provider.redirect_uri = microsoft_integration_config.REDIRECT_URI

    auth_url = await provider.get_authorization_url("state-token")

    query = parse_qs(urlparse(auth_url).query)
    assert "prompt" not in query
    assert query["client_id"] == ["client-id"]
    assert query["state"] == ["state-token"]
    assert query["scope"] == [
        "offline_access User.Read Team.ReadBasic.All Channel.ReadBasic.All Chat.Read"
    ]
