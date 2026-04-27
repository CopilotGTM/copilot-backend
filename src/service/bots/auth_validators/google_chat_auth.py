"""Validate Google Chat webhook requests using Google-issued bearer tokens.

Google Chat webhooks are validated by checking an ID token in `Authorization`.
This supports both Google OAuth2 verification and the legacy Chat JWT fallback used
in some environments, then enforces that the token origin is Chat itself.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, cast

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from src.infra.utils.logger import get_logger
from src.service.bots.auth_validators.base import AuthValidationResult

logger = get_logger(__name__)

CHAT_ISSUER = "chat@system.gserviceaccount.com"
CHAT_CERTS_URL = f"https://www.googleapis.com/service_accounts/v1/metadata/x509/{CHAT_ISSUER}"


class GoogleChatAuthValidator:
    """Validate bearer tokens for Google Chat interaction callbacks."""

    def __init__(self, audience: str | None = None) -> None:
        """Store the configured expected audience for token verification."""
        self._audience = audience or ""

    async def validate(
        self,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> AuthValidationResult:
        """Validate `Authorization` header bearer token.

        Returns:
            AuthValidationResult: success flag and payload/error metadata for caller
                decisions and logs.
        """
        del body
        authorization = headers.get("Authorization") or headers.get("authorization") or ""
        if not authorization.startswith("Bearer "):
            return AuthValidationResult(False, "missing_bearer_token")
        if not self._audience:
            return AuthValidationResult(False, "audience_not_configured")

        bearer = authorization.removeprefix("Bearer ").strip()
        if not bearer:
            return AuthValidationResult(False, "missing_bearer_token")

        try:
            payload = await asyncio.to_thread(self._verify_token, bearer)
        except Exception as exc:
            logger.warning("Google Chat token verification failed: %s", exc)
            return AuthValidationResult(False, "invalid_token")

        email = payload.get("email")
        issuer = payload.get("iss")
        if email == CHAT_ISSUER or issuer == CHAT_ISSUER:
            return AuthValidationResult(True, payload=payload)
        return AuthValidationResult(False, "invalid_issuer")

    def _verify_token(self, bearer: str) -> dict[str, Any]:
        """Verify token with OAuth2 first, then fallback to Chat issuer certs."""
        request = google_requests.Request()
        oauth_error: Exception | None = None
        try:
            return dict(
                cast(
                    "Mapping[str, Any]",
                    id_token.verify_oauth2_token(bearer, request, self._audience),
                )
            )
        except Exception as exc:
            oauth_error = exc

        try:
            return dict(
                id_token.verify_token(
                    bearer,
                    request,
                    self._audience,
                    certs_url=CHAT_CERTS_URL,
                )
            )
        except Exception as chat_jwt_error:
            raise ValueError(
                "Google Chat bearer token verification failed. "
                f"OAuth2 ID-token check failed: {oauth_error}; "
                f"Chat JWT check failed: {chat_jwt_error}"
            ) from chat_jwt_error
