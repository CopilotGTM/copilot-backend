import asyncio
import random
import urllib.parse
from typing import Any

import httpx
from src.domain.models.organisation_tables.sync import IntegrationType
from src.infra.config.configs import microsoft_integration_config
from src.infra.config.microsoft_integration_scopes import get_microsoft_integration_scopes
from src.infra.utils.logger import get_logger
from src.service.integrations.providers.base_provider import BaseIntegrationProvider

logger = get_logger()

_MAX_RETRIES = 3
_BASE_BACKOFF_SEC = 1.0
_MAX_BACKOFF_SEC = 30.0


class MicrosoftProvider(BaseIntegrationProvider):
    """Microsoft-specific implementation of the Integration Provider pattern.

    Handles OAuth via Azure AD v2.0 endpoints. Scopes vary by integration type
    (Outlook Email, Outlook Calendar, Teams). Uses "common" tenant to support
    both personal Microsoft accounts and work/school (Azure AD) accounts.

    Credentials (client_id, client_secret) are loaded lazily on the first
    async call — from env vars for local dev, or Key Vault for production.
    """

    AUTHORIZATION_BASE_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    USER_INFO_URL = "https://graph.microsoft.com/v1.0/me"

    def __init__(self, integration_type: IntegrationType):
        super().__init__(integration_type)
        self.redirect_uri = microsoft_integration_config.REDIRECT_URI
        self.timeout = microsoft_integration_config.MICROSOFT_HTTP_TIMEOUT_SEC
        self._client_id: str | None = None
        self._client_secret: str | None = None

    async def _ensure_credentials(self) -> None:
        """Load client_id and client_secret from env or Key Vault on first use."""
        if self._client_id and self._client_secret:
            return
        client_id, client_secret = await microsoft_integration_config.get_credentials()
        if not client_id or not client_secret:
            raise RuntimeError(
                "Microsoft OAuth credentials not available — "
                "set MICROSOFT_CLIENT_ID/MICROSOFT_CLIENT_SECRET env vars "
                "or store in Key Vault"
            )
        self._client_id = client_id
        self._client_secret = client_secret

    @property
    def provider_key(self) -> str:
        return "microsoft"

    def _get_scopes(self) -> list[str]:
        """Determine required scopes based on IntegrationType."""
        return list(get_microsoft_integration_scopes(self.integration_type))

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """HTTP request to a Microsoft endpoint with 429 rate-limit retry."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(_MAX_RETRIES + 1):
                if method == "POST":
                    response = await client.post(url, data=data, headers=headers)
                else:
                    response = await client.get(url, headers=headers)

                if response.status_code != 429 or attempt >= _MAX_RETRIES:
                    return response

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
                    "Microsoft OAuth 429 rate-limited on %s — retry %d/%d after %.1fs",
                    url,
                    attempt + 1,
                    _MAX_RETRIES,
                    total_wait,
                )
                await asyncio.sleep(total_wait)

        raise RuntimeError("Exhausted retries without response")

    async def get_authorization_url(self, state: str) -> str:
        await self._ensure_credentials()
        scopes = self._get_scopes()
        params = {
            "client_id": self._client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "response_mode": "query",
            "prompt": "consent",
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
            "scope": " ".join(self._get_scopes()),
        }

        try:
            response = await self._request_with_retry("POST", self.TOKEN_URL, data=data)
            response.raise_for_status()
            json_response = response.json()
            if isinstance(json_response, dict):
                return json_response
            return {}
        except httpx.HTTPStatusError as e:
            logger.error(f"Microsoft HTTP Error: {e.response.text}")
            raise Exception(f"Failed to exchange Microsoft OAuth code: {e}") from e
        except Exception as e:
            logger.error(f"Microsoft OAuth exchange error: {e}")
            raise

    async def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        await self._ensure_credentials()
        data = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
            "scope": " ".join(self._get_scopes()),
        }

        try:
            response = await self._request_with_retry("POST", self.TOKEN_URL, data=data)
            response.raise_for_status()
            json_response = response.json()
            if isinstance(json_response, dict):
                return json_response
            return {}
        except httpx.HTTPStatusError:
            # Re-raise as-is so _is_permanent_auth_error() can inspect the response body
            raise
        except Exception as e:
            logger.error(f"Microsoft OAuth refresh error: {e}")
            raise

    async def revoke_token(self, token: str) -> None:  # noqa: ARG002
        # Azure AD v2.0 does not expose a token revocation endpoint.
        # Disconnection is handled by deleting stored tokens on our side.
        logger.info(
            "Microsoft OAuth: token revocation is a no-op (Azure AD has no revoke endpoint)"
        )

    async def get_user_info(self, access_token: str) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            response = await self._request_with_retry("GET", self.USER_INFO_URL, headers=headers)
            response.raise_for_status()
            json_response = response.json()
            if not isinstance(json_response, dict):
                return {}
            # Normalize to match the contract expected by copilot_callback_service
            # (it reads user_info.get("email")). Microsoft Graph returns mail or
            # userPrincipalName — mail can be null for personal accounts.
            if "email" not in json_response:
                json_response["email"] = (
                    json_response.get("mail") or json_response.get("userPrincipalName") or ""
                )
            return json_response
        except Exception as e:
            logger.error(f"Failed to fetch Microsoft user info: {e}")
            raise
