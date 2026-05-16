from __future__ import annotations

import base64
import hashlib
import hmac
import time
from unittest.mock import AsyncMock

import pytest
from src.service.integrations.webhooks.hubspot.verifier import (
    HubSpotWebhookVerificationError,
    verify_hubspot_v3_signature,
)


def _signature(*, secret: str, method: str, uri: str, body: bytes, timestamp: str) -> str:
    source = method.upper().encode() + uri.encode() + body + timestamp.encode()
    digest = hmac.new(secret.encode(), source, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


@pytest.mark.asyncio
async def test_verify_hubspot_v3_signature_accepts_valid_signature(monkeypatch) -> None:
    secret = "hubspot-secret"
    body = b'[{"subscriptionType":"contact.privacyDeletion"}]'
    timestamp = str(int(time.time() * 1000))
    uri = "https://api.copilotgtm.com/users/integrations/hubspot/webhook"

    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.verifier."
        "hubspot_integration_config.get_credentials",
        AsyncMock(return_value=("hubspot-client-id", secret)),
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.verifier."
        "hubspot_integration_config.WEBHOOK_PUBLIC_URL",
        "",
    )

    assert await verify_hubspot_v3_signature(
        method="POST",
        request_uri=uri,
        body=body,
        timestamp=timestamp,
        signature=_signature(
            secret=secret,
            method="POST",
            uri=uri,
            body=body,
            timestamp=timestamp,
        ),
    )


@pytest.mark.asyncio
async def test_verify_hubspot_v3_signature_loads_secret_inside_verifier(monkeypatch) -> None:
    secret = "hubspot-secret"
    body = b'[{"subscriptionType":"deal.creation"}]'
    timestamp = str(int(time.time() * 1000))
    uri = "https://api.copilotgtm.com/users/integrations/hubspot/webhook"
    get_credentials = AsyncMock(return_value=("hubspot-client-id", secret))

    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.verifier."
        "hubspot_integration_config.get_credentials",
        get_credentials,
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.verifier."
        "hubspot_integration_config.WEBHOOK_PUBLIC_URL",
        "",
    )

    assert await verify_hubspot_v3_signature(
        method="POST",
        request_uri=uri,
        body=body,
        timestamp=timestamp,
        signature=_signature(
            secret=secret,
            method="POST",
            uri=uri,
            body=body,
            timestamp=timestamp,
        ),
    )
    get_credentials.assert_awaited_once()


@pytest.mark.asyncio
async def test_verify_hubspot_v3_signature_uses_configured_public_url(
    monkeypatch,
) -> None:
    secret = "hubspot-secret"
    body = b"[]"
    timestamp = str(int(time.time() * 1000))
    signed_uri = "https://api.copilotgtm.com/users/integrations/hubspot/webhook"
    internal_uri = "http://testserver/users/integrations/hubspot/webhook"

    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.verifier."
        "hubspot_integration_config.get_credentials",
        AsyncMock(return_value=("hubspot-client-id", secret)),
    )
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.verifier."
        "hubspot_integration_config.WEBHOOK_PUBLIC_URL",
        signed_uri,
    )

    assert await verify_hubspot_v3_signature(
        method="POST",
        request_uri=internal_uri,
        body=body,
        timestamp=timestamp,
        signature=_signature(
            secret=secret,
            method="POST",
            uri=signed_uri,
            body=body,
            timestamp=timestamp,
        ),
    )


@pytest.mark.asyncio
async def test_verify_hubspot_v3_signature_rejects_stale_timestamp(monkeypatch) -> None:
    timestamp = str(int((time.time() - 600) * 1000))

    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.verifier."
        "hubspot_integration_config.get_credentials",
        AsyncMock(return_value=("hubspot-client-id", "hubspot-secret")),
    )

    with pytest.raises(HubSpotWebhookVerificationError, match="timestamp"):
        await verify_hubspot_v3_signature(
            method="POST",
            request_uri="https://api.copilotgtm.com/users/integrations/hubspot/webhook",
            body=b"[]",
            timestamp=timestamp,
            signature="bad",
        )


@pytest.mark.asyncio
async def test_verify_hubspot_v3_signature_rejects_mismatch(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.service.integrations.webhooks.hubspot.verifier."
        "hubspot_integration_config.get_credentials",
        AsyncMock(return_value=("hubspot-client-id", "hubspot-secret")),
    )

    with pytest.raises(HubSpotWebhookVerificationError, match="signature"):
        await verify_hubspot_v3_signature(
            method="POST",
            request_uri="https://api.copilotgtm.com/users/integrations/hubspot/webhook",
            body=b"[]",
            timestamp=str(int(time.time() * 1000)),
            signature="bad",
        )
