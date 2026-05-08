from __future__ import annotations

import base64
import hashlib
import hmac
import time

import pytest
from src.service.integrations.webhooks.hubspot.verifier import (
    HubSpotWebhookVerificationError,
    verify_hubspot_v3_signature,
)


def _signature(*, secret: str, method: str, uri: str, body: bytes, timestamp: str) -> str:
    source = method.upper().encode() + uri.encode() + body + timestamp.encode()
    digest = hmac.new(secret.encode(), source, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def test_verify_hubspot_v3_signature_accepts_valid_signature() -> None:
    secret = "hubspot-secret"
    body = b'[{"subscriptionType":"contact.privacyDeletion"}]'
    timestamp = str(int(time.time() * 1000))
    uri = "https://api.copilotgtm.com/users/integrations/hubspot/webhook"

    assert verify_hubspot_v3_signature(
        client_secret=secret,
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


def test_verify_hubspot_v3_signature_rejects_stale_timestamp() -> None:
    timestamp = str(int((time.time() - 600) * 1000))

    with pytest.raises(HubSpotWebhookVerificationError, match="timestamp"):
        verify_hubspot_v3_signature(
            client_secret="hubspot-secret",
            method="POST",
            request_uri="https://api.copilotgtm.com/users/integrations/hubspot/webhook",
            body=b"[]",
            timestamp=timestamp,
            signature="bad",
        )


def test_verify_hubspot_v3_signature_rejects_mismatch() -> None:
    with pytest.raises(HubSpotWebhookVerificationError, match="signature"):
        verify_hubspot_v3_signature(
            client_secret="hubspot-secret",
            method="POST",
            request_uri="https://api.copilotgtm.com/users/integrations/hubspot/webhook",
            body=b"[]",
            timestamp=str(int(time.time() * 1000)),
            signature="bad",
        )
