from unittest.mock import AsyncMock

import httpx
import pytest
from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.providers.google_provider import GoogleProvider


def test_google_provider_gmail_scopes_are_read_only() -> None:
    provider = GoogleProvider(IntegrationType.GMAIL)

    assert provider._get_scopes() == [
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
        "https://www.googleapis.com/auth/gmail.readonly",
    ]


def test_google_provider_calendar_scopes_include_google_meet() -> None:
    provider = GoogleProvider(IntegrationType.GOOGLE_CALENDAR)

    assert provider._get_scopes() == [
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/meetings.space.readonly",
    ]


@pytest.mark.asyncio
async def test_refresh_token_reloads_credentials_and_retries_once_on_401(monkeypatch) -> None:
    provider = GoogleProvider(IntegrationType.GMAIL)
    provider._client_id = "old-client-id"
    provider._client_secret = "old-client-secret"

    request = httpx.Request("POST", provider.TOKEN_URL)
    unauthorized_response = httpx.Response(401, request=request, text="unauthorized")
    success_response = httpx.Response(
        200,
        request=request,
        json={"access_token": "new-access", "expires_in": 3600},
    )
    request_with_retry = AsyncMock(side_effect=[unauthorized_response, success_response])
    reload_credentials = AsyncMock(
        side_effect=lambda: setattr(provider, "_client_id", "new-client-id")
        or setattr(provider, "_client_secret", "new-client-secret")
    )

    monkeypatch.setattr(provider, "_request_with_retry", request_with_retry)
    monkeypatch.setattr(provider, "_reload_credentials", reload_credentials)

    result = await provider.refresh_token("stored-refresh-token")

    assert result["access_token"] == "new-access"
    assert request_with_retry.await_count == 2
    reload_credentials.assert_awaited_once()
    second_call_data = request_with_retry.await_args_list[1].kwargs["data"]
    assert second_call_data["client_id"] == "new-client-id"
    assert second_call_data["client_secret"] == "new-client-secret"
