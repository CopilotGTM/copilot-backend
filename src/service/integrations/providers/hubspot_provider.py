from __future__ import annotations

import asyncio
import random
import urllib.parse
from typing import Any

import httpx
from src.domain.models.organisation_tables.sync import IntegrationType
from src.infra.config.configs import hubspot_integration_config
from src.infra.utils.logger import get_logger
from src.service.integrations.providers.base_provider import BaseIntegrationProvider

logger = get_logger(__name__)

_MAX_RETRIES = 3
_BASE_BACKOFF_SEC = 1.0
_MAX_BACKOFF_SEC = 30.0


class HubSpotProvider(BaseIntegrationProvider):
    """HubSpot-specific implementation of the integration provider pattern."""

    AUTHORIZATION_BASE_URL = "https://app.hubspot.com/oauth/authorize"
    TOKEN_URL = "https://api.hubapi.com/oauth/v1/token"
    # HubSpot's current revoke endpoint is versioned; the old v1/token/revoke
    # path returns 404 during disconnect.
    REVOKE_URL = "https://api.hubapi.com/oauth/2026-03/token/revoke"
    ACCESS_TOKEN_INFO_URL = "https://api.hubapi.com/oauth/v1/access-tokens/{token}"

    def __init__(self, integration_type: IntegrationType):
        super().__init__(integration_type)
        self.redirect_uri = hubspot_integration_config.REDIRECT_URI
        self.timeout = hubspot_integration_config.HUBSPOT_HTTP_TIMEOUT_SEC
        self._client_id: str | None = None
        self._client_secret: str | None = None

    async def _ensure_credentials(self) -> None:
        """Load client_id and client_secret from env or Key Vault on first use."""
        if self._client_id and self._client_secret:
            return

        client_id, client_secret = await hubspot_integration_config.get_credentials()
        if not client_id or not client_secret:
            raise RuntimeError(
                "HubSpot OAuth credentials not available — "
                "set HUBSPOT_CLIENT_ID/HUBSPOT_CLIENT_SECRET env vars "
                "or store in Key Vault"
            )

        self._client_id = client_id
        self._client_secret = client_secret

    @property
    def provider_key(self) -> str:
        return "hubspot"

    def _get_scopes(self) -> list[str]:
        return [
            "oauth",
            "automation",
            "conversations.read",
            "sales-email-read",
            "crm.objects.companies.read",
            "crm.objects.companies.write",
            "crm.objects.contacts.read",
            "crm.objects.contacts.write",
            "crm.objects.deals.read",
            "crm.objects.deals.write",
            "crm.objects.leads.read",
            "crm.objects.leads.write",
            "crm.objects.line_items.read",
            "crm.objects.line_items.write",
            "crm.objects.products.read",
            "crm.objects.products.write",
            "crm.objects.owners.read",
            "crm.objects.users.read",
            "crm.schemas.companies.read",
            "crm.schemas.contacts.read",
            "crm.schemas.deals.read",
        ]

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """HTTP request to a HubSpot endpoint with 429 rate-limit retry."""
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
                    "HubSpot OAuth 429 rate-limited on %s — retry %d/%d after %.1fs",
                    url,
                    attempt + 1,
                    _MAX_RETRIES,
                    total_wait,
                )
                await asyncio.sleep(total_wait)

        raise RuntimeError("Exhausted retries without response")

    async def get_authorization_url(self, state: str) -> str:
        await self._ensure_credentials()
        params = {
            "client_id": self._client_id,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(self._get_scopes()),
            "state": state,
        }
        return f"{self.AUTHORIZATION_BASE_URL}?{urllib.parse.urlencode(params)}"

    async def exchange_code(self, code: str, redirect_uri: str | None = None) -> dict[str, Any]:
        await self._ensure_credentials()
        response = await self._request_with_retry(
            "POST",
            self.TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "redirect_uri": redirect_uri or self.redirect_uri,
                "code": code,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        json_response = response.json()
        if isinstance(json_response, dict):
            return json_response
        raise ValueError("HubSpot OAuth exchange returned an invalid payload")

    async def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        await self._ensure_credentials()
        response = await self._request_with_retry(
            "POST",
            self.TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": refresh_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        json_response = response.json()
        if isinstance(json_response, dict):
            return json_response
        raise ValueError("HubSpot token refresh returned an invalid payload")

    async def revoke_token(self, token: str) -> None:
        await self._ensure_credentials()
        response = await self._request_with_retry(
            "POST",
            self.REVOKE_URL,
            data={
                "token": token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()

    async def get_user_info(self, access_token: str) -> dict[str, Any]:
        response = await self._request_with_retry(
            "GET",
            self.ACCESS_TOKEN_INFO_URL.format(token=access_token),
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response.raise_for_status()
        json_response = response.json()
        if not isinstance(json_response, dict):
            raise ValueError("HubSpot access token info returned an invalid payload")

        email = str(json_response.get("user") or json_response.get("user_email") or "").strip()
        email = email.lower()
        if not email:
            raise ValueError("HubSpot OAuth response did not include a connected account email")

        return {
            "email": email,
            "hubspot_user_id": json_response.get("user_id"),
            "hubspot_portal_id": json_response.get("hub_id") or json_response.get("hub_domain"),
            "hubspot_portal_name": json_response.get("hub_domain"),
        }
