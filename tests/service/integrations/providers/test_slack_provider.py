import pytest
from src.domain.models.organisation_tables.sync import IntegrationType
from src.infra.config.configs import slack_config
from src.service.integrations.providers.slack_provider import SlackProvider


def test_slack_provider_scopes_cover_channel_sync() -> None:
    provider = SlackProvider(IntegrationType.SLACK)

    assert provider._get_scopes() == [
        "channels:read",
        "groups:read",
        "mpim:read",
        "channels:history",
        "groups:history",
        "mpim:history",
        "users:read",
        "users:read.email",
        "team:read",
    ]


@pytest.mark.asyncio
async def test_slack_provider_uses_slack_redirect_uri_in_authorization_url(
    monkeypatch,
) -> None:
    provider = SlackProvider(IntegrationType.SLACK)

    async def _fake_get_credentials() -> tuple[str | None, str | None, str | None, str | None]:
        return (
            "signing-secret",
            "app-id",
            "integration-client-id",
            "integration-client-secret",
        )

    redirect_uri = "https://app.test/users/integrations/copilot-oauth/callback"
    monkeypatch.setattr(slack_config, "REDIRECT_URI", redirect_uri)
    monkeypatch.setattr(slack_config, "get_credentials", _fake_get_credentials)

    provider.redirect_uri = slack_config.REDIRECT_URI
    auth_url = await provider.get_authorization_url("state-123")

    assert "client_id=integration-client-id" in auth_url
    assert (
        "redirect_uri=https%3A%2F%2Fapp.test%2Fusers%2Fintegrations%2Fcopilot-oauth%2Fcallback"
        in auth_url
    )
    assert "state=state-123" in auth_url
