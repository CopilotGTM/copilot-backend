import asyncio
import base64
import random
import urllib.parse
from typing import Any

import httpx
from src.domain.models.organisation_tables.sync import IntegrationType
from src.infra.config.configs import slack_config
from src.infra.utils.logger import get_logger
from src.service.integrations.providers.base_provider import BaseIntegrationProvider

logger = get_logger(__name__)

_MAX_RETRIES = 3
_BASE_BACKOFF_SEC = 1.0
_MAX_BACKOFF_SEC = 30.0


class SlackProvider(BaseIntegrationProvider):
    """Slack-specific implementation of the Integration Provider pattern."""

    AUTHORIZATION_BASE_URL = "https://slack.com/oauth/v2/authorize"
    TOKEN_URL = "https://slack.com/api/oauth.v2.access"
    AUTH_TEST_URL = "https://slack.com/api/auth.test"
    USER_INFO_URL = "https://slack.com/api/users.info"
    TEAM_INFO_URL = "https://slack.com/api/team.info"
    REVOKE_URL = "https://slack.com/api/auth.revoke"

    def __init__(self, integration_type: IntegrationType):
        super().__init__(integration_type)
        self.timeout = 10.0
        self._client_id: str | None = None
        self._client_secret: str | None = None
        self.redirect_uri = slack_config.REDIRECT_URI

    async def _ensure_credentials(self) -> None:
        """Load client_id and client_secret from env or Key Vault on first use."""
        if self._client_id and self._client_secret:
            return

        _, _, client_id, client_secret = await slack_config.get_credentials()
        if not client_id or not client_secret:
            raise RuntimeError(
                "Slack OAuth credentials not available — set Slack client ID/client secret "
                "or store them in Key Vault"
            )

        self._client_id = client_id
        self._client_secret = client_secret

    @property
    def provider_key(self) -> str:
        return "slack"

    def _get_scopes(self) -> list[str]:
        """Determine required Slack user scopes."""
        return [
            "channels:read",
            "groups:read",
            "mpim:read",
            "channels:history",
            "groups:history",
            "mpim:history",
            "users:read",
            "users:read.email",
            "team:read",
        ]

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        auth_basic: bool = False,
    ) -> httpx.Response:
        """HTTP request to a Slack endpoint with 429 rate-limit retry."""
        request_headers = dict(headers or {})
        if auth_basic:
            await self._ensure_credentials()
            basic = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode(
                "utf-8"
            )
            request_headers["Authorization"] = f"Basic {basic}"

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(_MAX_RETRIES + 1):
                if method == "POST":
                    response = await client.post(
                        url, data=data, params=params, headers=request_headers
                    )
                else:
                    response = await client.get(url, params=params, headers=request_headers)

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
                    "Slack OAuth 429 rate-limited on %s — retry %d/%d after %.1fs",
                    url,
                    attempt + 1,
                    _MAX_RETRIES,
                    total_wait,
                )
                await asyncio.sleep(total_wait)

        raise RuntimeError("Exhausted retries without response")

    @staticmethod
    def _require_ok(response_json: dict[str, Any], *, context: str) -> dict[str, Any]:
        """Validate Slack Web API response shape."""
        if response_json.get("ok"):
            return response_json
        error = response_json.get("error") or "unknown_error"
        raise ValueError(f"Slack {context} failed: {error}")

    async def get_authorization_url(self, state: str) -> str:
        await self._ensure_credentials()
        params = {
            "client_id": self._client_id,
            "redirect_uri": self.redirect_uri,
            "user_scope": ",".join(self._get_scopes()),
            "state": state,
        }
        return f"{self.AUTHORIZATION_BASE_URL}?{urllib.parse.urlencode(params)}"

    async def exchange_code(self, code: str, redirect_uri: str | None = None) -> dict[str, Any]:
        await self._ensure_credentials()
        response = await self._request_with_retry(
            "POST",
            self.TOKEN_URL,
            data={
                "code": code,
                "redirect_uri": redirect_uri or self.redirect_uri,
            },
            auth_basic=True,
        )
        response.raise_for_status()

        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Slack token exchange returned an invalid payload")
        payload = self._require_ok(payload, context="oauth.v2.access")

        authed_user = payload.get("authed_user", {})
        if not isinstance(authed_user, dict):
            raise ValueError("Slack OAuth response missing authed_user payload")

        access_token = authed_user.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ValueError("Slack OAuth response missing authed user access token")

        team = payload.get("team", {})
        if not isinstance(team, dict):
            team = {}

        normalized: dict[str, Any] = {
            "access_token": access_token,
            "refresh_token": authed_user.get("refresh_token"),
            "expires_in": authed_user.get("expires_in"),
            "token_type": authed_user.get("token_type", "user"),
            "scope": authed_user.get("scope"),
            "slack_user_id": authed_user.get("id"),
            "slack_team_id": team.get("id"),
            "slack_team_name": team.get("name"),
        }

        if payload.get("access_token"):
            normalized["bot_access_token"] = payload.get("access_token")
        if payload.get("bot_user_id"):
            normalized["bot_user_id"] = payload.get("bot_user_id")
        if authed_user.get("refresh_token_expires_in") is not None:
            normalized["refresh_token_expires_in"] = authed_user.get("refresh_token_expires_in")

        return normalized

    async def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        await self._ensure_credentials()
        response = await self._request_with_retry(
            "POST",
            self.TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            auth_basic=True,
        )
        response.raise_for_status()

        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Slack token refresh returned an invalid payload")
        payload = self._require_ok(payload, context="token refresh")

        normalized: dict[str, Any] = {
            "access_token": payload.get("access_token"),
            "refresh_token": payload.get("refresh_token"),
            "expires_in": payload.get("expires_in"),
            "token_type": payload.get("token_type", "user"),
            "scope": payload.get("scope"),
        }
        if payload.get("refresh_token_expires_in") is not None:
            normalized["refresh_token_expires_in"] = payload.get("refresh_token_expires_in")
        return normalized

    async def revoke_token(self, token: str) -> None:
        response = await self._request_with_retry(
            "POST",
            self.REVOKE_URL,
            data={"token": token},
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            self._require_ok(payload, context="auth.revoke")

    async def get_user_info(self, access_token: str) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {access_token}"}

        auth_test = await self._request_with_retry("GET", self.AUTH_TEST_URL, headers=headers)
        auth_test.raise_for_status()
        auth_payload = auth_test.json()
        if not isinstance(auth_payload, dict):
            raise ValueError("Slack auth.test returned an invalid payload")
        auth_payload = self._require_ok(auth_payload, context="auth.test")

        user_id = str(auth_payload.get("user_id") or "")
        team_id = str(auth_payload.get("team_id") or "")
        if not user_id:
            raise ValueError("Slack auth.test did not return a user_id")

        user_info = await self._request_with_retry(
            "GET",
            self.USER_INFO_URL,
            params={"user": user_id},
            headers=headers,
        )
        user_info.raise_for_status()
        user_payload = user_info.json()
        if not isinstance(user_payload, dict):
            raise ValueError("Slack users.info returned an invalid payload")
        user_payload = self._require_ok(user_payload, context="users.info")

        team_name: str | None = None
        if team_id:
            team_info = await self._request_with_retry("GET", self.TEAM_INFO_URL, headers=headers)
            team_info.raise_for_status()
            team_payload = team_info.json()
            if isinstance(team_payload, dict) and team_payload.get("ok"):
                team = team_payload.get("team", {})
                if isinstance(team, dict):
                    team_name = team.get("name")

        user = user_payload.get("user", {})
        if not isinstance(user, dict):
            user = {}
        profile = user.get("profile", {})
        if not isinstance(profile, dict):
            profile = {}

        email = profile.get("email") or ""
        display_name = (
            profile.get("display_name")
            or profile.get("real_name")
            or user.get("real_name")
            or user.get("name")
        )

        return {
            "email": email,
            "display_name": display_name,
            "slack_user_id": user_id,
            "slack_team_id": team_id,
            "slack_team_name": team_name,
        }
