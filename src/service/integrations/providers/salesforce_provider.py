from __future__ import annotations

import asyncio
import random
import urllib.parse
from typing import Any

import httpx
from src.domain.models.organisation_tables.sync import IntegrationType
from src.infra.config.configs import salesforce_integration_config
from src.infra.utils.logger import get_logger
from src.service.integrations.providers.base_provider import BaseIntegrationProvider

logger = get_logger(__name__)

_MAX_RETRIES = 3
_BASE_BACKOFF_SEC = 1.0
_MAX_BACKOFF_SEC = 30.0
_DEFAULT_ACCESS_TOKEN_EXPIRES_IN = 3600

_PRODUCTION_OAUTH_BASE_URL = "https://login.salesforce.com"
_SALESFORCE_OAUTH_PATHS = {
    "authorize": "/services/oauth2/authorize",
    "token": "/services/oauth2/token",
    "revoke": "/services/oauth2/revoke",
    "userinfo": "/services/oauth2/userinfo",
}


def _normalize_salesforce_oauth_host(host: str) -> str:
    if host.endswith(".lightning.force.com"):
        return f"{host.removesuffix('.lightning.force.com')}.my.salesforce.com"
    return host


def normalize_salesforce_oauth_base_url(oauth_base_url: str | None = None) -> str:
    """Return a safe Salesforce OAuth host for one connection attempt."""
    raw_url = (oauth_base_url or "").strip()
    if not raw_url:
        return _PRODUCTION_OAUTH_BASE_URL

    candidate = raw_url if "://" in raw_url else f"https://{raw_url}"
    parsed = urllib.parse.urlparse(candidate)
    host = _normalize_salesforce_oauth_host((parsed.hostname or "").lower())

    if (
        parsed.scheme != "https"
        or not host
        or parsed.username
        or parsed.password
        or parsed.port
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Salesforce OAuth URL must be an HTTPS Salesforce login/My Domain host")

    if host in {"login.salesforce.com", "test.salesforce.com"} or host.endswith(
        ".my.salesforce.com"
    ):
        return f"https://{host}"

    raise ValueError("Salesforce OAuth URL must be a Salesforce login/My Domain host")


def resolve_salesforce_oauth_base_url(
    metadata: dict[str, Any],
    credentials: dict[str, Any],
) -> str | None:
    """Resolve the stored OAuth host while preserving old metadata compatibility."""
    raw_oauth_base_url = (
        metadata.get("oauth_base_url")
        or metadata.get("salesforce_oauth_base_url")
        or credentials.get("oauth_base_url")
        or credentials.get("salesforce_oauth_base_url")
    )
    if not raw_oauth_base_url:
        return None
    return normalize_salesforce_oauth_base_url(str(raw_oauth_base_url))


def salesforce_oauth_endpoint(oauth_base_url: str | None, endpoint: str) -> str:
    base_url = normalize_salesforce_oauth_base_url(oauth_base_url)
    return f"{base_url}{_SALESFORCE_OAUTH_PATHS[endpoint]}"


def _with_default_token_expiry(payload: dict[str, Any]) -> dict[str, Any]:
    if (
        payload.get("access_token")
        and payload.get("expires_in") is None
        and payload.get("expires_at") is None
    ):
        normalized = dict(payload)
        normalized["expires_in"] = _DEFAULT_ACCESS_TOKEN_EXPIRES_IN
        return normalized
    return payload


class SalesforceProvider(BaseIntegrationProvider):
    """Salesforce-specific implementation of the integration provider pattern."""

    AUTHORIZATION_BASE_URL = salesforce_oauth_endpoint(None, "authorize")
    TOKEN_URL = salesforce_oauth_endpoint(None, "token")
    REVOKE_URL = salesforce_oauth_endpoint(None, "revoke")
    USERINFO_URL = salesforce_oauth_endpoint(None, "userinfo")

    def __init__(self, integration_type: IntegrationType):
        super().__init__(integration_type)
        self.redirect_uri = salesforce_integration_config.REDIRECT_URI
        self.timeout = salesforce_integration_config.SALESFORCE_HTTP_TIMEOUT_SEC
        self._client_id: str | None = None
        self._client_secret: str | None = None

    async def _ensure_credentials(self) -> None:
        """Load client_id and client_secret from env or Key Vault on first use."""
        if self._client_id and self._client_secret:
            return

        client_id, client_secret = await salesforce_integration_config.get_credentials()
        if not client_id or not client_secret:
            raise RuntimeError(
                "Salesforce OAuth credentials not available — "
                "set SALESFORCE_CLIENT_ID/SALESFORCE_CLIENT_SECRET env vars "
                "or store in Key Vault"
            )

        self._client_id = client_id
        self._client_secret = client_secret

    @property
    def provider_key(self) -> str:
        return "salesforce"

    def _get_scopes(self) -> list[str]:
        return ["refresh_token", "offline_access", "api", "openid", "profile", "email"]

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """HTTP request to a Salesforce endpoint with 429 rate-limit retry."""
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
                    "Salesforce OAuth 429 rate-limited on %s — retry %d/%d after %.1fs",
                    url,
                    attempt + 1,
                    _MAX_RETRIES,
                    total_wait,
                )
                await asyncio.sleep(total_wait)

        raise RuntimeError("Exhausted retries without response")

    async def get_authorization_url(
        self,
        state: str,
        *,
        oauth_base_url: str | None = None,
    ) -> str:
        await self._ensure_credentials()
        params = {
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(self._get_scopes()),
            "state": state,
        }
        authorize_url = salesforce_oauth_endpoint(oauth_base_url, "authorize")
        return f"{authorize_url}?{urllib.parse.urlencode(params)}"

    async def exchange_code(
        self,
        code: str,
        redirect_uri: str | None = None,
        *,
        oauth_base_url: str | None = None,
    ) -> dict[str, Any]:
        await self._ensure_credentials()
        response = await self._request_with_retry(
            "POST",
            salesforce_oauth_endpoint(oauth_base_url, "token"),
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
            return _with_default_token_expiry(json_response)
        raise ValueError("Salesforce OAuth exchange returned an invalid payload")

    async def refresh_token(
        self,
        refresh_token: str,
        *,
        oauth_base_url: str | None = None,
    ) -> dict[str, Any]:
        await self._ensure_credentials()
        response = await self._request_with_retry(
            "POST",
            salesforce_oauth_endpoint(oauth_base_url, "token"),
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
            return _with_default_token_expiry(json_response)
        raise ValueError("Salesforce token refresh returned an invalid payload")

    async def revoke_token(self, token: str, *, oauth_base_url: str | None = None) -> None:
        response = await self._request_with_retry(
            "POST",
            salesforce_oauth_endpoint(oauth_base_url, "revoke"),
            data={"token": token},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()

    async def get_user_info(
        self,
        access_token: str,
        *,
        oauth_base_url: str | None = None,
    ) -> dict[str, Any]:
        response = await self._request_with_retry(
            "GET",
            salesforce_oauth_endpoint(oauth_base_url, "userinfo"),
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response.raise_for_status()
        json_response = response.json()
        if not isinstance(json_response, dict):
            raise ValueError("Salesforce userinfo returned an invalid payload")

        email = str(
            json_response.get("email") or json_response.get("preferred_username") or ""
        ).strip()
        email = email.lower()
        if not email:
            raise ValueError("Salesforce OAuth response did not include a connected account email")

        return {
            "email": email,
            "salesforce_user_id": json_response.get("user_id"),
            "salesforce_organization_id": json_response.get("organization_id"),
            "salesforce_instance_url": json_response.get("custom_domain"),
            "salesforce_username": json_response.get("preferred_username")
            or json_response.get("email"),
        }
