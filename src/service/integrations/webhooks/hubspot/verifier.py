from __future__ import annotations

import base64
import hashlib
import hmac
import time

from src.infra.config.configs import hubspot_integration_config


class HubSpotWebhookVerificationError(ValueError):
    """Raised when a HubSpot webhook signature cannot be trusted."""


def _request_uri_for_signature(request_uri: str) -> str:
    configured_url = getattr(hubspot_integration_config, "WEBHOOK_PUBLIC_URL", "")
    return configured_url or request_uri


async def verify_hubspot_v3_signature(
    *,
    method: str,
    request_uri: str,
    body: bytes,
    timestamp: str | None,
    signature: str | None,
    now: float | None = None,
    tolerance_seconds: int = 300,
) -> bool:
    """Verify HubSpot v3 request signatures using verifier-owned config."""

    _, client_secret = await hubspot_integration_config.get_credentials()
    if not client_secret:
        raise HubSpotWebhookVerificationError("HubSpot client secret is not configured")
    if not timestamp or not signature:
        raise HubSpotWebhookVerificationError("HubSpot signature headers are missing")

    try:
        timestamp_ms = int(timestamp)
    except ValueError as exc:
        raise HubSpotWebhookVerificationError("HubSpot timestamp is invalid") from exc

    current_time = time.time() if now is None else now
    request_time = timestamp_ms / 1000
    if abs(current_time - request_time) > tolerance_seconds:
        raise HubSpotWebhookVerificationError("HubSpot timestamp is outside the allowed window")

    source = method.upper().encode("utf-8")
    source += _request_uri_for_signature(request_uri).encode("utf-8")
    source += body
    source += timestamp.encode("utf-8")
    expected = base64.b64encode(
        hmac.new(client_secret.encode("utf-8"), source, hashlib.sha256).digest()
    ).decode("utf-8")

    if not hmac.compare_digest(expected, signature):
        raise HubSpotWebhookVerificationError("HubSpot signature mismatch")

    return True
