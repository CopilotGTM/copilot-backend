from urllib.parse import parse_qs, urlparse

import pytest
from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.providers.hubspot_provider import HubSpotProvider


@pytest.mark.asyncio
async def test_hubspot_provider_builds_authorization_url() -> None:
    provider = HubSpotProvider(IntegrationType.HUBSPOT)
    provider._client_id = "hubspot-client-id"
    provider._client_secret = "hubspot-client-secret"
    provider.redirect_uri = (
        "https://app.example.com/api/v1/users/integrations/copilot-oauth/callback"
    )

    url = await provider.get_authorization_url("signed-state")

    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    assert parsed.netloc == "app.hubspot.com"
    assert params["client_id"] == ["hubspot-client-id"]
    assert params["state"] == ["signed-state"]
    assert params["redirect_uri"] == [provider.redirect_uri]
    assert params["scope"] == [
        (
            "oauth automation conversations.read sales-email-read "
            "crm.objects.companies.read crm.objects.companies.write "
            "crm.objects.contacts.read crm.objects.contacts.write "
            "crm.objects.deals.read crm.objects.deals.write "
            "crm.objects.leads.read crm.objects.leads.write "
            "crm.objects.line_items.read crm.objects.line_items.write "
            "crm.objects.products.read crm.objects.products.write "
            "crm.objects.owners.read crm.objects.users.read "
            "crm.schemas.companies.read crm.schemas.contacts.read crm.schemas.deals.read"
        )
    ]


@pytest.mark.asyncio
async def test_hubspot_provider_normalizes_user_info(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = HubSpotProvider(IntegrationType.HUBSPOT)

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "user": "sales@acme.com",
                "user_id": 42,
                "hub_id": 777,
                "hub_domain": "acme",
            }

    async def fake_request(*args, **kwargs) -> FakeResponse:
        return FakeResponse()

    monkeypatch.setattr(provider, "_request_with_retry", fake_request)

    payload = await provider.get_user_info("token")

    assert payload["email"] == "sales@acme.com"
    assert payload["hubspot_user_id"] == 42
    assert payload["hubspot_portal_id"] == 777
    assert payload["hubspot_portal_name"] == "acme"


@pytest.mark.asyncio
async def test_hubspot_provider_revokes_token_with_current_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = HubSpotProvider(IntegrationType.HUBSPOT)
    provider._client_id = "hubspot-client-id"
    provider._client_secret = "hubspot-client-secret"
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

    async def fake_request(
        method: str,
        url: str,
        *,
        data: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> FakeResponse:
        captured["method"] = method
        captured["url"] = url
        captured["data"] = data
        captured["headers"] = headers
        return FakeResponse()

    monkeypatch.setattr(provider, "_request_with_retry", fake_request)

    await provider.revoke_token("refresh-token")

    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.hubapi.com/oauth/2026-03/token/revoke"
    assert captured["data"] == {
        "token": "refresh-token",
        "client_id": "hubspot-client-id",
        "client_secret": "hubspot-client-secret",
    }
    assert captured["headers"] == {"Content-Type": "application/x-www-form-urlencoded"}
