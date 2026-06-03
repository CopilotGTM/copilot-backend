import asyncio
import random
import urllib.parse
from typing import Any

import httpx
from src.domain.models.organisation_tables.sync import IntegrationType
from src.infra.config.configs import google_integration_config
from src.infra.config.google_integration_scopes import get_google_integration_scopes
from src.infra.utils.logger import get_logger
from src.service.integrations.providers.base_provider import BaseIntegrationProvider

logger = get_logger()

# 429 retry — Google OAuth endpoints share the same quota pool as data APIs.
# Token refresh during a sync can be rate-limited under heavy concurrent load.
_MAX_RETRIES = 3
_BASE_BACKOFF_SEC = 1.0
_MAX_BACKOFF_SEC = 30.0


class GoogleProvider(BaseIntegrationProvider):
    """
    Google-specific implementation of the Integration Provider pattern.
    Handles scopes dynamically based on whether it is Google Mail or Google Calendar.

    Credentials (client_id, client_secret) are loaded lazily on the first
    async call — from env vars for local dev, or Key Vault for production.

    All HTTP calls include 429 rate-limit retry with exponential backoff.
    """

    AUTHORIZATION_BASE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_URL = "https://oauth2.googleapis.com/token"
    USER_INFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
    REVOKE_URL = "https://oauth2.googleapis.com/revoke"

    def __init__(self, integration_type: IntegrationType):
        super().__init__(integration_type)
        self.redirect_uri = google_integration_config.REDIRECT_URI
        self.timeout = google_integration_config.GOOGLE_HTTP_TIMEOUT_SEC
        # Lazy-loaded on first async call via _ensure_credentials()
        self._client_id: str | None = None
        self._client_secret: str | None = None

    async def _ensure_credentials(self) -> None:
        """Load client_id and client_secret from env or Key Vault on first use."""
        if self._client_id and self._client_secret:
            return
        client_id, client_secret = await google_integration_config.get_credentials()
        if not client_id or not client_secret:
            raise RuntimeError(
                "Google OAuth credentials not available — "
                "set GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET env vars "
                "or store in Key Vault"
            )
        self._client_id = client_id
        self._client_secret = client_secret

    async def _reload_credentials(self) -> None:
        """Bypass the TTL cache after Google rejects the current app credential pair."""
        client_id, client_secret = await google_integration_config.refresh_credentials()
        if not client_id or not client_secret:
            raise RuntimeError("Google OAuth credentials not available after forced reload")
        self._client_id = client_id
        self._client_secret = client_secret

    @property
    def provider_key(self) -> str:
        return "google"

    def _get_scopes(self, *, include_gmail_read_scope: bool = True) -> list[str]:
        """Determine required scopes based on IntegrationType"""
        return list(
            get_google_integration_scopes(
                self.integration_type,
                include_gmail_read_scope=include_gmail_read_scope,
            )
        )

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """HTTP request to a Google endpoint with 429 rate-limit retry.

        Handles transient rate-limiting on OAuth token endpoints (refresh,
        exchange, revoke) which share quota with data APIs. Respects
        Retry-After header when provided by Google.

        Returns the raw httpx.Response — caller is responsible for
        raise_for_status() and parsing.
        """
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(_MAX_RETRIES + 1):
                if method == "POST":
                    response = await client.post(url, data=data, headers=headers)
                else:
                    response = await client.get(url, headers=headers)

                if response.status_code != 429 or attempt >= _MAX_RETRIES:
                    return response

                # Rate-limited — back off and retry
                retry_after = response.headers.get("Retry-After")
                if retry_after is not None:
                    try:
                        wait = min(float(retry_after), _MAX_BACKOFF_SEC)
                    except (ValueError, TypeError):
                        wait = min(_BASE_BACKOFF_SEC * (2**attempt), _MAX_BACKOFF_SEC)
                else:
                    wait = min(_BASE_BACKOFF_SEC * (2**attempt), _MAX_BACKOFF_SEC)

                jitter = random.uniform(0, wait * 0.5)  # noqa: S311  # nosec B311
                total_wait = wait + jitter

                logger.warning(
                    "Google OAuth 429 rate-limited on %s — retry %d/%d after %.1fs",
                    url,
                    attempt + 1,
                    _MAX_RETRIES,
                    total_wait,
                )
                await asyncio.sleep(total_wait)

        # Unreachable — the loop always returns on the last attempt.
        # Satisfies type checker.
        raise RuntimeError("Exhausted retries without response")

    async def get_authorization_url(
        self,
        state: str,
        *,
        include_gmail_read_scope: bool = True,
    ) -> str:
        await self._ensure_credentials()
        scopes = self._get_scopes(include_gmail_read_scope=include_gmail_read_scope)
        params = {
            "client_id": self._client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "access_type": "offline",
            "prompt": "consent",  # Force consent to ensure we get a refresh token
            "include_granted_scopes": (
                "false"
                if self.integration_type == IntegrationType.GMAIL and not include_gmail_read_scope
                else "true"
            ),
            "state": state,
        }
        url = f"{self.AUTHORIZATION_BASE_URL}?{urllib.parse.urlencode(params)}"
        return url

    async def exchange_code(self, code: str, redirect_uri: str | None = None) -> dict[str, Any]:
        await self._ensure_credentials()
        data = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri or self.redirect_uri,
        }

        try:
            response = await self._request_with_retry("POST", self.TOKEN_URL, data=data)
            response.raise_for_status()
            json_response = response.json()
            if isinstance(json_response, dict):
                return json_response
            return {}
        except httpx.HTTPStatusError as e:
            logger.error(f"Google HTTP Error: {e.response.text}")
            raise Exception(f"Failed to exchange Google OAuth code: {e}") from e
        except Exception as e:
            logger.error(f"Google OAuth exchange error: {e}")
            raise

    async def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        await self._ensure_credentials()
        data = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }

        try:
            response = await self._request_with_retry("POST", self.TOKEN_URL, data=data)
            response.raise_for_status()
            json_response = response.json()
            if isinstance(json_response, dict):
                return json_response
            return {}
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                logger.warning(
                    "Google token refresh returned 401; reloading OAuth app credentials once"
                )
                await self._reload_credentials()
                data["client_id"] = self._client_id
                data["client_secret"] = self._client_secret
                response = await self._request_with_retry("POST", self.TOKEN_URL, data=data)
                response.raise_for_status()
                json_response = response.json()
                if isinstance(json_response, dict):
                    return json_response
                return {}

            # Re-raise as-is so _is_permanent_auth_error() can inspect the response body
            raise
        except Exception as e:
            logger.error(f"Google OAuth refresh error: {e}")
            raise

    async def revoke_token(self, token: str) -> None:
        response = await self._request_with_retry(
            "POST",
            self.REVOKE_URL,
            data={"token": token},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()

    async def get_user_info(self, access_token: str) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            response = await self._request_with_retry("GET", self.USER_INFO_URL, headers=headers)
            response.raise_for_status()
            json_response = response.json()
            if isinstance(json_response, dict):
                return json_response
            return {}
        except Exception as e:
            logger.error(f"Failed to fetch Google user info: {e}")
            raise
