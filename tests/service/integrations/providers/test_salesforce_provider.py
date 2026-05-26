from urllib.parse import parse_qs, urlparse

import pytest
from src.domain.models.organisation_tables.sync import IntegrationType
from src.service.integrations.providers.salesforce_provider import (
    SalesforceProvider,
    normalize_salesforce_oauth_base_url,
)


@pytest.mark.asyncio
async def test_salesforce_provider_builds_authorization_url() -> None:
    provider = SalesforceProvider(IntegrationType.SALESFORCE)
    provider._client_id = "salesforce-client-id"
    provider._client_secret = "salesforce-client-secret"
    provider.redirect_uri = (
        "https://app.example.com/api/v1/users/integrations/copilot-oauth/callback"
    )

    url = await provider.get_authorization_url("signed-state")

    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    assert parsed.netloc == "login.salesforce.com"
    assert params["client_id"] == ["salesforce-client-id"]
    assert params["state"] == ["signed-state"]
    assert params["redirect_uri"] == [provider.redirect_uri]
    assert params["scope"] == ["refresh_token offline_access api openid profile email"]


@pytest.mark.parametrize(
    ("raw_url", "expected"),
    [
        (
            "https://ameyo--gtmcopilot.sandbox.lightning.force.com/lightning/page/home",
            "https://ameyo--gtmcopilot.sandbox.my.salesforce.com",
        ),
        (
            "https://ameyo--gtmcopilot.sandbox.my.salesforce.com/lightning/page/home",
            "https://ameyo--gtmcopilot.sandbox.my.salesforce.com",
        ),
        ("https://login.salesforce.com", "https://login.salesforce.com"),
        ("https://test.salesforce.com/", "https://test.salesforce.com"),
        ("", "https://login.salesforce.com"),
    ],
)
def test_normalize_salesforce_oauth_base_url_accepts_salesforce_hosts_and_pages(
    raw_url: str,
    expected: str,
) -> None:
    assert normalize_salesforce_oauth_base_url(raw_url) == expected


@pytest.mark.parametrize(
    "raw_url",
    [
        "http://test.salesforce.com",
        "https://evil.example.com",
        "https://user:test@test.salesforce.com",
        "https://test.salesforce.com:8443",
    ],
)
def test_normalize_salesforce_oauth_base_url_rejects_unsafe_hosts(raw_url: str) -> None:
    with pytest.raises(ValueError):
        normalize_salesforce_oauth_base_url(raw_url)


@pytest.mark.asyncio
async def test_salesforce_provider_normalizes_user_info(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = SalesforceProvider(IntegrationType.SALESFORCE)

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "user_id": "005ABC",
                "organization_id": "00DABC",
                "email": "sales@acme.com",
                "preferred_username": "sales@acme.com",
                "custom_domain": "https://acme.my.salesforce.com",
            }

    async def fake_request(*args, **kwargs) -> FakeResponse:
        return FakeResponse()

    monkeypatch.setattr(provider, "_request_with_retry", fake_request)

    payload = await provider.get_user_info("token")

    assert payload["email"] == "sales@acme.com"
    assert payload["salesforce_user_id"] == "005ABC"
    assert payload["salesforce_organization_id"] == "00DABC"
    assert payload["salesforce_instance_url"] == "https://acme.my.salesforce.com"
    assert payload["salesforce_username"] == "sales@acme.com"
