from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.api.dependencies.organisation_dependencies import UserIDs
from src.domain.models.master_tables.bot_platform_installation_models import (
    BotPlatformProvider,
)
from src.domain.models.master_tables.external_identity_models import (
    ExternalIdentityProvider,
)


class _ScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None


class _ExecuteResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _ScalarResult(self._rows)


class _FakeSession:
    def __init__(self, rows_by_provider):
        self.rows_by_provider = rows_by_provider
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        text = str(stmt)
        if "bot_platform_installations" in text:
            return _ExecuteResult(self.rows_by_provider.get("installations", []))
        if "external_identity_mappings" in text:
            return _ExecuteResult(self.rows_by_provider.get("mappings", []))
        return _ExecuteResult([])


def _user_ids() -> UserIDs:
    return UserIDs(
        user_id=uuid4(),
        organisation_id=uuid4(),
        user_email="user@example.com",
        user_name="Test User",
    )


@pytest.mark.asyncio
async def test_list_available_bots_returns_slack_install_and_identity_status():
    from src.service.integrations.bot_integration_service import BotIntegrationService

    user_ids = _user_ids()
    installation_id = uuid4()
    mapping_id = uuid4()
    installed_at = datetime(2026, 5, 11, 1, 2, 3, tzinfo=UTC)

    session = _FakeSession(
        {
            "installations": [
                SimpleNamespace(
                    installation_id=installation_id,
                    provider=BotPlatformProvider.SLACK,
                    external_workspace_id="T123",
                    external_workspace_name="Acme Slack",
                    installed_at=installed_at,
                    last_used_at=None,
                )
            ],
            "mappings": [
                SimpleNamespace(
                    mapping_id=mapping_id,
                    provider=ExternalIdentityProvider.SLACK,
                )
            ],
        }
    )

    service = BotIntegrationService(session=session, user_ids=user_ids)

    response = await service.list_available_bots()

    assert response.total_available == 1
    assert response.total_installed == 1
    assert response.total_linked == 1
    assert len(response.bots) == 1
    slack = response.bots[0]
    assert slack.provider == "slack"
    assert slack.display_name == "CopilotGTM Slack Bot"
    assert slack.connection_mode == "oauth_install"
    assert slack.installed is True
    assert slack.identity_linked is True
    assert slack.action_required == "none"
    assert slack.installation is not None
    assert slack.installation.workspace_id == "T123"
    assert slack.installation.workspace_name == "Acme Slack"
    assert slack.identity_mapping_id == mapping_id


@pytest.mark.asyncio
async def test_get_slack_connect_url_uses_bot_callback_and_bot_scopes(monkeypatch):
    from src.service.integrations import bot_integration_service
    from src.service.integrations.bot_integration_service import BotIntegrationService

    async def fake_get_credentials():
        return "signing-secret", "A123", "client-123", "client-secret"

    monkeypatch.setattr(
        bot_integration_service.slack_config,
        "get_credentials",
        fake_get_credentials,
    )
    monkeypatch.setattr(
        bot_integration_service.slack_config,
        "SLACK_BOT_REDIRECT_URI",
        "https://api.example.com/users/bots/slack/oauth/callback",
        raising=False,
    )

    service = BotIntegrationService(session=_FakeSession({}), user_ids=_user_ids())

    response = await service.get_slack_connect_url()

    assert response.provider == "slack"
    assert response.expires_at is None
    assert response.connect_url.startswith("https://slack.com/oauth/v2/authorize?")
    assert "client_id=client-123" in response.connect_url
    assert (
        "redirect_uri=https%3A%2F%2Fapi.example.com%2Fusers%2Fbots%2Fslack%2Foauth%2Fcallback"
        in response.connect_url
    )
    assert "scope=" in response.connect_url
    assert "chat%3Awrite" in response.connect_url
    assert "app_mentions%3Aread" in response.connect_url
