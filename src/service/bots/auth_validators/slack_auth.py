"""Slack webhook authentication via request signing.

Slack signs every request with HMAC-SHA256 using the app's Signing Secret.
We verify the signature to ensure the request is genuinely from Slack.

See: https://api.slack.com/authentication/verifying-requests-from-slack
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping

from src.infra.utils.logger import get_logger
from src.service.bots.auth_validators.base import AuthValidationResult, WebhookAuthValidator

logger = get_logger(__name__)

# Slack requests older than 5 minutes are rejected (replay attack protection)
MAX_REQUEST_AGE_SECONDS = 300


class SlackAuthValidator(WebhookAuthValidator):
    """Validate Slack webhook requests using signing secret verification."""

    def __init__(self, signing_secret: str) -> None:
        self._signing_secret = signing_secret

    async def validate(
        self, headers: Mapping[str, str], body: bytes | None = None
    ) -> AuthValidationResult:
        """Validate Slack request signature.

        Slack sends three headers for verification:
        - X-Slack-Request-Timestamp: Unix epoch timestamp
        - X-Slack-Signature: v0=<HMAC-SHA256 hex digest>

        The signature is computed as:
            HMAC-SHA256(signing_secret, "v0:{timestamp}:{body}")
        """
        timestamp = headers.get("X-Slack-Request-Timestamp", "")
        signature = headers.get("X-Slack-Signature", "")

        if not timestamp or not signature:
            return AuthValidationResult(False, "missing_slack_headers")

        # Reject requests older than 5 minutes (replay protection)
        try:
            request_time = int(timestamp)
        except ValueError:
            return AuthValidationResult(False, "invalid_timestamp")

        if abs(time.time() - request_time) > MAX_REQUEST_AGE_SECONDS:
            return AuthValidationResult(False, "request_too_old")

        # Compute expected signature
        if body is None:
            body = b""

        sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
        expected_signature = (
            "v0="
            + hmac.new(
                self._signing_secret.encode("utf-8"),
                sig_basestring.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        )

        # Constant-time comparison to prevent timing attacks
        if not hmac.compare_digest(expected_signature, signature):
            return AuthValidationResult(False, "invalid_signature")

        return AuthValidationResult(True)
