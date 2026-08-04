"""Tests for CopilotOAuthService availability validation."""

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables.sync import IntegrationType
from src.infra.auth.user_context import UserIDs
from src.service.integrations.copilot_oauth_service import (
    CopilotOAuthIntegrationUnavailableError,
    CopilotOAuthService,
)


@dataclass
class FakeMetadata:
    """Minimal metadata shape needed by CopilotOAuthService tests."""

    availability_status: str


def _make_user_ids() -> UserIDs:
    return UserIDs(
        user_id=uuid4(),
        organisation_id=uuid4(),
        user_name="Test User",
        user_email="test@example.com",
    )


@pytest.mark.asyncio
async def test_initiate_oauth_flow_rejects_unavailable_integration() -> None:
    service = CopilotOAuthService()

    with (
        patch(
            "src.service.integrations.copilot_oauth_service.get_integration_metadata",
            return_value=FakeMetadata(availability_status="coming_soon"),
        ),
        pytest.raises(CopilotOAuthIntegrationUnavailableError) as exc_info,
    ):
        await service.initiate_oauth_flow(
            user_ids=_make_user_ids(),
            integration_type=IntegrationType.GMAIL,
        )

    assert exc_info.value.status == "coming_soon"


@pytest.mark.asyncio
async def test_initiate_oauth_flow_allows_beta_integration() -> None:
    service = CopilotOAuthService()
    fake_provider = AsyncMock()
    fake_provider.get_authorization_url.return_value = "https://example.com/oauth"

    with (
        patch(
            "src.service.integrations.copilot_oauth_service.get_integration_metadata",
            return_value=FakeMetadata(availability_status="beta"),
        ),
        patch(
            "src.service.integrations.copilot_oauth_service.security_secrets_config.get_oauth_state_secret",
            new=AsyncMock(return_value="oauth-signing-secret"),
        ),
        patch(
            "src.service.integrations.copilot_oauth_service.ProviderFactory.get_provider",
            return_value=fake_provider,
        ),
    ):
        result = await service.initiate_oauth_flow(
            user_ids=_make_user_ids(),
            integration_type=IntegrationType.GOOGLE_CALENDAR,
        )
        state_token = fake_provider.get_authorization_url.await_args.args[0]
        decoded = await CopilotOAuthService.decode_oauth_state(state_token)

    assert result.auth_url == "https://example.com/oauth"
    assert result.attempt_id
    fake_provider.get_authorization_url.assert_awaited_once()
    assert decoded["jti"] == result.attempt_id


@pytest.mark.asyncio
async def test_initiate_gmail_oauth_ignores_org_gmail_read_scope_preference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = CopilotOAuthService()
    user_ids = _make_user_ids()

    async def fake_master_session():
        yield SimpleNamespace(
            get=AsyncMock(
                return_value=SimpleNamespace(
                    preferences={"integrations": {"google": {"gmail_read_enabled": False}}}
                )
            )
        )

    monkeypatch.setattr(
        "src.service.integrations.copilot_oauth_service.acquire_master_async_session",
        fake_master_session,
    )

    with (
        patch(
            "src.service.integrations.copilot_oauth_service.get_integration_metadata",
            return_value=FakeMetadata(availability_status="beta"),
        ),
        patch(
            "src.service.integrations.copilot_oauth_service.security_secrets_config.get_oauth_state_secret",
            new=AsyncMock(return_value="oauth-signing-secret"),
        ),
        patch(
            "src.service.integrations.providers.google_provider.google_integration_config.get_credentials",
            new=AsyncMock(return_value=("client-id", "client-secret")),
        ),
    ):
        result = await service.initiate_oauth_flow(
            user_ids=user_ids,
            integration_type=IntegrationType.GMAIL,
        )

    query = parse_qs(urlparse(result.auth_url).query)
    requested_scopes = set(query["scope"][0].split())

    assert requested_scopes == {
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.compose",
    }
    assert query["include_granted_scopes"] == ["true"]


@pytest.mark.asyncio
async def test_initiate_gmail_oauth_includes_read_scope_with_org_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = CopilotOAuthService()
    user_ids = _make_user_ids()

    async def fake_master_session():
        yield SimpleNamespace(
            get=AsyncMock(
                return_value=SimpleNamespace(
                    preferences={"integrations": {"google": {"gmail_read_enabled": True}}}
                )
            )
        )

    monkeypatch.setattr(
        "src.service.integrations.copilot_oauth_service.acquire_master_async_session",
        fake_master_session,
    )

    with (
        patch(
            "src.service.integrations.copilot_oauth_service.get_integration_metadata",
            return_value=FakeMetadata(availability_status="beta"),
        ),
        patch(
            "src.service.integrations.copilot_oauth_service.security_secrets_config.get_oauth_state_secret",
            new=AsyncMock(return_value="oauth-signing-secret"),
        ),
        patch(
            "src.service.integrations.providers.google_provider.google_integration_config.get_credentials",
            new=AsyncMock(return_value=("client-id", "client-secret")),
        ),
    ):
        result = await service.initiate_oauth_flow(
            user_ids=user_ids,
            integration_type=IntegrationType.GMAIL,
        )

    query = parse_qs(urlparse(result.auth_url).query)
    requested_scopes = set(query["scope"][0].split())

    assert "https://www.googleapis.com/auth/gmail.readonly" in requested_scopes
    assert "https://www.googleapis.com/auth/gmail.send" in requested_scopes
    assert query["include_granted_scopes"] == ["true"]


@pytest.mark.asyncio
async def test_initiate_google_oauth_uses_org_gmail_read_scope_preference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = CopilotOAuthService()
    user_ids = _make_user_ids()

    async def fake_master_session():
        yield SimpleNamespace(
            get=AsyncMock(
                return_value=SimpleNamespace(
                    preferences={"integrations": {"google": {"gmail_read_enabled": False}}}
                )
            )
        )

    monkeypatch.setattr(
        "src.service.integrations.copilot_oauth_service.acquire_master_async_session",
        fake_master_session,
    )

    with (
        patch(
            "src.service.integrations.copilot_oauth_service.get_integration_metadata",
            return_value=FakeMetadata(availability_status="beta"),
        ),
        patch(
            "src.service.integrations.copilot_oauth_service.security_secrets_config.get_oauth_state_secret",
            new=AsyncMock(return_value="oauth-signing-secret"),
        ),
        patch(
            "src.service.integrations.providers.google_provider.google_integration_config.get_credentials",
            new=AsyncMock(return_value=("client-id", "client-secret")),
        ),
    ):
        result = await service.initiate_oauth_flow(
            user_ids=user_ids,
            integration_type=IntegrationType.GOOGLE,
        )

    query = parse_qs(urlparse(result.auth_url).query)
    requested_scopes = set(query["scope"][0].split())

    assert "https://www.googleapis.com/auth/calendar.readonly" in requested_scopes
    assert "https://www.googleapis.com/auth/gmail.send" in requested_scopes
    assert "https://www.googleapis.com/auth/gmail.readonly" not in requested_scopes
    assert query["include_granted_scopes"] == ["false"]


@pytest.mark.asyncio
async def test_initiate_microsoft_admin_consent_flow_builds_adminconsent_url() -> None:
    service = CopilotOAuthService()

    with (
        patch(
            "src.service.integrations.copilot_oauth_service.get_integration_metadata",
            return_value=FakeMetadata(availability_status="beta"),
        ),
        patch(
            "src.service.integrations.copilot_oauth_service.security_secrets_config.get_oauth_state_secret",
            new=AsyncMock(return_value="oauth-signing-secret"),
        ),
        patch(
            "src.service.integrations.providers.microsoft_provider.microsoft_integration_config.get_credentials",
            new=AsyncMock(return_value=("client-id", "client-secret")),
        ),
        patch(
            "src.service.integrations.providers.microsoft_provider.microsoft_integration_config.REDIRECT_URI",
            "https://api.example.com/users/integrations/copilot-oauth/callback",
        ),
    ):
        result = await service.initiate_microsoft_admin_consent_flow(
            user_ids=_make_user_ids(),
            integration_type=IntegrationType.OUTLOOK_CALENDAR,
        )
        parsed = urlparse(result.auth_url)
        query = parse_qs(parsed.query)
        decoded = await CopilotOAuthService.decode_oauth_state(query["state"][0])

    assert parsed.scheme == "https"
    assert parsed.netloc == "login.microsoftonline.com"
    assert parsed.path == "/organizations/v2.0/adminconsent"
    assert query["client_id"] == ["client-id"]
    assert query["redirect_uri"] == [
        "https://api.example.com/users/integrations/copilot-oauth/callback"
    ]
    assert query["scope"] == [
        "offline_access https://graph.microsoft.com/User.Read "
        "https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/Mail.ReadWrite "
        "https://graph.microsoft.com/Mail.Send "
        "https://graph.microsoft.com/Calendars.ReadWrite "
        "https://graph.microsoft.com/OnlineMeetings.Read "
        "https://graph.microsoft.com/Team.ReadBasic.All "
        "https://graph.microsoft.com/Channel.ReadBasic.All "
        "https://graph.microsoft.com/Chat.Read"
    ]
    assert decoded["type"] == IntegrationType.OUTLOOK_CALENDAR.value
    assert decoded["purpose"] == "microsoft_admin_consent"
    assert decoded["jti"] == result.attempt_id


@pytest.mark.asyncio
async def test_initiate_microsoft_admin_consent_flow_rejects_non_microsoft() -> None:
    service = CopilotOAuthService()

    with pytest.raises(ValueError, match="Admin consent is only supported for Microsoft"):
        await service.initiate_microsoft_admin_consent_flow(
            user_ids=_make_user_ids(),
            integration_type=IntegrationType.GMAIL,
        )


@pytest.mark.asyncio
async def test_handle_microsoft_admin_consent_callback_rejects_ungranted_response() -> None:
    service = CopilotOAuthService()

    with pytest.raises(ValueError, match="Microsoft admin consent was not granted"):
        await service.handle_microsoft_admin_consent_callback(
            state="unused",
            admin_consent="False",
            tenant="tenant-id",
            scope="User.Read",
        )


@pytest.mark.asyncio
async def test_initiate_salesforce_oauth_flow_includes_validated_custom_base_in_state() -> None:
    service = CopilotOAuthService()
    fake_provider = AsyncMock()
    fake_provider.get_authorization_url.return_value = "https://example.com/oauth"

    with (
        patch(
            "src.service.integrations.copilot_oauth_service.get_integration_metadata",
            return_value=FakeMetadata(availability_status="beta"),
        ),
        patch(
            "src.service.integrations.copilot_oauth_service.security_secrets_config.get_oauth_state_secret",
            new=AsyncMock(return_value="oauth-signing-secret"),
        ),
        patch(
            "src.service.integrations.copilot_oauth_service.ProviderFactory.get_provider",
            return_value=fake_provider,
        ),
    ):
        await service.initiate_oauth_flow(
            user_ids=_make_user_ids(),
            integration_type=IntegrationType.SALESFORCE,
            oauth_base_url="https://ameyo--gtmcopilot.sandbox.my.salesforce.com",
        )
        state_token = fake_provider.get_authorization_url.await_args.args[0]
        oauth_base_url = fake_provider.get_authorization_url.await_args.kwargs["oauth_base_url"]
        decoded = await CopilotOAuthService.decode_oauth_state(state_token)

    assert oauth_base_url == "https://ameyo--gtmcopilot.sandbox.my.salesforce.com"
    assert decoded["oauth_base_url"] == oauth_base_url


@pytest.mark.asyncio
async def test_initiate_salesforce_oauth_flow_defaults_to_production_login() -> None:
    service = CopilotOAuthService()
    fake_provider = AsyncMock()
    fake_provider.get_authorization_url.return_value = "https://example.com/oauth"

    with (
        patch(
            "src.service.integrations.copilot_oauth_service.get_integration_metadata",
            return_value=FakeMetadata(availability_status="beta"),
        ),
        patch(
            "src.service.integrations.copilot_oauth_service.security_secrets_config.get_oauth_state_secret",
            new=AsyncMock(return_value="oauth-signing-secret"),
        ),
        patch(
            "src.service.integrations.copilot_oauth_service.ProviderFactory.get_provider",
            return_value=fake_provider,
        ),
    ):
        await service.initiate_oauth_flow(
            user_ids=_make_user_ids(),
            integration_type=IntegrationType.SALESFORCE,
        )

    assert (
        fake_provider.get_authorization_url.await_args.kwargs["oauth_base_url"]
        == "https://login.salesforce.com"
    )


@pytest.mark.asyncio
async def test_initiate_salesforce_oauth_flow_normalizes_lightning_url() -> None:
    service = CopilotOAuthService()
    fake_provider = AsyncMock()
    fake_provider.get_authorization_url.return_value = "https://example.com/oauth"

    with (
        patch(
            "src.service.integrations.copilot_oauth_service.get_integration_metadata",
            return_value=FakeMetadata(availability_status="beta"),
        ),
        patch(
            "src.service.integrations.copilot_oauth_service.security_secrets_config.get_oauth_state_secret",
            new=AsyncMock(return_value="oauth-signing-secret"),
        ),
        patch(
            "src.service.integrations.copilot_oauth_service.ProviderFactory.get_provider",
            return_value=fake_provider,
        ),
    ):
        await service.initiate_oauth_flow(
            user_ids=_make_user_ids(),
            integration_type=IntegrationType.SALESFORCE,
            oauth_base_url=(
                "https://ameyo--gtmcopilot.sandbox.lightning.force.com/lightning/page/home"
            ),
        )
        state_token = fake_provider.get_authorization_url.await_args.args[0]
        oauth_base_url = fake_provider.get_authorization_url.await_args.kwargs["oauth_base_url"]
        decoded = await CopilotOAuthService.decode_oauth_state(state_token)

    assert oauth_base_url == "https://ameyo--gtmcopilot.sandbox.my.salesforce.com"
    assert decoded["oauth_base_url"] == oauth_base_url


@pytest.mark.asyncio
async def test_generate_and_decode_salesforce_state_carries_oauth_base_url() -> None:
    user_ids = _make_user_ids()

    with patch(
        "src.service.integrations.copilot_oauth_service.security_secrets_config.get_oauth_state_secret",
        new=AsyncMock(return_value="oauth-signing-secret"),
    ):
        token = await CopilotOAuthService.generate_oauth_state(
            user_ids,
            IntegrationType.SALESFORCE,
            oauth_base_url="https://acme.my.salesforce.com",
        )
        decoded = await CopilotOAuthService.decode_oauth_state(token)

    assert decoded["oauth_base_url"] == "https://acme.my.salesforce.com"


@pytest.mark.asyncio
async def test_generate_and_decode_oauth_state_uses_runtime_secret() -> None:
    user_ids = _make_user_ids()

    with patch(
        "src.service.integrations.copilot_oauth_service.security_secrets_config.get_oauth_state_secret",
        new=AsyncMock(return_value="oauth-signing-secret"),
    ):
        token = await CopilotOAuthService.generate_oauth_state(user_ids, IntegrationType.GMAIL)
        decoded = await CopilotOAuthService.decode_oauth_state(token)

    assert decoded["sub"] == str(user_ids.user_id)
    assert decoded["org"] == str(user_ids.organisation_id)
    assert decoded["type"] == IntegrationType.GMAIL.value
    assert decoded["jti"]


@pytest.mark.asyncio
async def test_salesforce_provider_uses_custom_oauth_base_for_authorize() -> None:
    from src.service.integrations.providers.salesforce_provider import SalesforceProvider

    provider = SalesforceProvider(IntegrationType.SALESFORCE)

    with patch(
        "src.service.integrations.providers.salesforce_provider.salesforce_integration_config.get_credentials",
        new=AsyncMock(return_value=("client-id", "client-secret")),
    ):
        url = await provider.get_authorization_url(
            "state-token",
            oauth_base_url="https://ameyo--gtmcopilot.sandbox.my.salesforce.com",
        )

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "ameyo--gtmcopilot.sandbox.my.salesforce.com"
    assert parsed.path == "/services/oauth2/authorize"
    assert query["client_id"] == ["client-id"]
    assert query["state"] == ["state-token"]


def test_salesforce_oauth_base_rejects_url_parser_tricks() -> None:
    from src.service.integrations.providers.salesforce_provider import (
        normalize_salesforce_oauth_base_url,
    )

    invalid_urls = [
        "https://evil.com@foo.my.salesforce.com",
        "https://login.salesforce.com\\@evil.com",
        "https://login.salesforce.com%2f.evil.com",
        "https://foo.my.salesforce.com.",
    ]

    for url in invalid_urls:
        with pytest.raises(ValueError, match="Salesforce OAuth URL"):
            normalize_salesforce_oauth_base_url(url)


@pytest.mark.asyncio
async def test_generate_oauth_state_requires_runtime_secret() -> None:
    with (
        patch(
            "src.service.integrations.copilot_oauth_service.security_secrets_config.get_oauth_state_secret",
            new=AsyncMock(side_effect=RuntimeError("missing secret")),
        ),
        pytest.raises(RuntimeError, match="missing secret"),
    ):
        await CopilotOAuthService.generate_oauth_state(_make_user_ids(), IntegrationType.GMAIL)
