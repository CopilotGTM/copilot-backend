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
