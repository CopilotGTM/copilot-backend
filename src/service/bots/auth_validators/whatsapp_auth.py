"""Validate WhatsApp webhooks with Meta app-secret signing.

The validator is used by webhook handlers before any payload-specific work:
it confirms the webhook sender using `X-Hub-Signature-256` and protects the
service from forged callbacks.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping

from src.service.bots.auth_validators.base import AuthValidationResult, WebhookAuthValidator


class WhatsAppAuthValidator(WebhookAuthValidator):
    """Validate inbound WhatsApp webhook requests from Meta.

    The caller passes raw request headers and body bytes so the HMAC can be
    recomputed exactly as Meta sent it.
    """

    def __init__(self, app_secret: str | None) -> None:
        """Store the app secret used to validate webhook signatures."""
        self._app_secret = app_secret or ""

    async def validate(
        self, headers: Mapping[str, str], body: bytes | None = None
    ) -> AuthValidationResult:
        """Check `X-Hub-Signature-256` using constant-time comparison.

        Returns:
            AuthValidationResult: validation outcome and a short reason string
                when validation fails.
        """
        if not self._app_secret:
            return AuthValidationResult(False, "app_secret_missing")

        signature = headers.get("X-Hub-Signature-256", "")
        if not signature.startswith("sha256="):
            return AuthValidationResult(False, "missing_signature")

        if body is None:
            body = b""

        expected = hmac.new(
            self._app_secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
        actual = signature[len("sha256=") :]
        if not hmac.compare_digest(expected, actual):
            return AuthValidationResult(False, "invalid_signature")

        return AuthValidationResult(True)
