"""Tests for CopilotOAuthService availability validation."""

from dataclasses import dataclass
from unittest.mock import AsyncMock, patch
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

    assert result == "https://example.com/oauth"
    fake_provider.get_authorization_url.assert_awaited_once()


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
async def test_generate_oauth_state_requires_runtime_secret() -> None:
    with (
        patch(
            "src.service.integrations.copilot_oauth_service.security_secrets_config.get_oauth_state_secret",
            new=AsyncMock(side_effect=RuntimeError("missing secret")),
        ),
        pytest.raises(RuntimeError, match="missing secret"),
    ):
        await CopilotOAuthService.generate_oauth_state(_make_user_ids(), IntegrationType.GMAIL)
