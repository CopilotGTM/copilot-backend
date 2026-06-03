from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.providers.salesforce_provider import SalesforceProvider


def _salesforce_provider() -> SalesforceProvider:
    provider = SalesforceProvider(IntegrationType.SALESFORCE)
    provider._client_id = "client-id"
    provider._client_secret = "client-secret"
    return provider


@pytest.mark.asyncio
async def test_exchange_code_defaults_expiry_when_salesforce_omits_it() -> None:
    provider = _salesforce_provider()
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "https://login.salesforce.com/services/oauth2/token"),
        json={"access_token": "access-token", "refresh_token": "refresh-token"},
    )

    with (
        patch.object(provider, "_ensure_credentials", AsyncMock()),
        patch.object(provider, "_request_with_retry", AsyncMock(return_value=response)),
    ):
        result = await provider.exchange_code("code")

    assert result["expires_in"] == 3600


@pytest.mark.asyncio
async def test_refresh_token_defaults_expiry_when_salesforce_omits_it() -> None:
    provider = _salesforce_provider()
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "https://login.salesforce.com/services/oauth2/token"),
        json={"access_token": "access-token"},
    )

    with (
        patch.object(provider, "_ensure_credentials", AsyncMock()),
        patch.object(provider, "_request_with_retry", AsyncMock(return_value=response)),
    ):
        result = await provider.refresh_token("refresh-token")

    assert result["expires_in"] == 3600
