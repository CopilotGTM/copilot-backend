import urllib.parse
from typing import Any

import httpx
from src.domain.models.organisation_tables.sync import IntegrationType
from src.infra.config.configs import google_integration_config
from src.infra.utils.logger import get_logger
from src.service.integrations.providers.base_provider import BaseIntegrationProvider

logger = get_logger()


class GoogleProvider(BaseIntegrationProvider):
    """
    Google-specific implementation of the Integration Provider pattern.
    Handles scopes dynamically based on whether it is Google Mail or Google Calendar.
    """

    AUTHORIZATION_BASE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_URL = "https://oauth2.googleapis.com/token"
    USER_INFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
    REVOKE_URL = "https://oauth2.googleapis.com/revoke"

    def __init__(self, integration_type: IntegrationType):
        super().__init__(integration_type)
        google_integration_config.validate()
        self.client_id = google_integration_config.GOOGLE_CLIENT_ID
        self.client_secret = google_integration_config.GOOGLE_CLIENT_SECRET
        self.redirect_uri = google_integration_config.REDIRECT_URI
        self.timeout = google_integration_config.GOOGLE_HTTP_TIMEOUT_SEC

    @property
    def provider_key(self) -> str:
        return "google"

    def _get_scopes(self) -> list[str]:
        """Determine required scopes based on IntegrationType"""
        base_scopes = [
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/userinfo.profile",
        ]
        if self.integration_type == IntegrationType.GMAIL:
            base_scopes.append("https://www.googleapis.com/auth/gmail.readonly")
        elif self.integration_type == IntegrationType.GOOGLE_CALENDAR:
            base_scopes.append("https://www.googleapis.com/auth/calendar.readonly")

        return base_scopes

    async def get_authorization_url(self, state: str) -> str:
        scopes = self._get_scopes()
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "access_type": "offline",
            "prompt": "consent",  # Force consent to ensure we get a refresh token
            "include_granted_scopes": "true",
            "state": state,
        }
        url = f"{self.AUTHORIZATION_BASE_URL}?{urllib.parse.urlencode(params)}"
        return url

    async def exchange_code(self, code: str, redirect_uri: str | None = None) -> dict[str, Any]:
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri or self.redirect_uri,
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.post(self.TOKEN_URL, data=data)
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
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.post(self.TOKEN_URL, data=data)
                response.raise_for_status()
                json_response = response.json()
                if isinstance(json_response, dict):
                    return json_response
                return {}
            except httpx.HTTPStatusError as e:
                logger.error(f"Google HTTP refresh error: {e.response.text}")
                raise Exception(f"Failed to refresh Google OAuth token: {e}") from e
            except Exception as e:
                logger.error(f"Google OAuth refresh error: {e}")
                raise

    async def revoke_token(self, token: str) -> None:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                self.REVOKE_URL,
                data={"token": token},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response.raise_for_status()

    async def get_user_info(self, access_token: str) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {access_token}"}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.get(self.USER_INFO_URL, headers=headers)
                response.raise_for_status()
                json_response = response.json()
                if isinstance(json_response, dict):
                    return json_response
                return {}
            except Exception as e:
                logger.error(f"Failed to fetch Google user info: {e}")
                raise
