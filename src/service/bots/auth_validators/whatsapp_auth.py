"""WhatsApp webhook authentication via Meta app-secret signing."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping

from src.service.bots.auth_validators.base import AuthValidationResult, WebhookAuthValidator


class WhatsAppAuthValidator(WebhookAuthValidator):
    """Validate Meta webhook requests using X-Hub-Signature-256."""

    def __init__(self, app_secret: str | None) -> None:
        self._app_secret = app_secret or ""

    async def validate(
        self, headers: Mapping[str, str], body: bytes | None = None
    ) -> AuthValidationResult:
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
