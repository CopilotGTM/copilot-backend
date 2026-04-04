from abc import ABC, abstractmethod
from typing import Any

from src.domain.models.organisation_tables.sync import IntegrationType


class BaseIntegrationProvider(ABC):
    """
    Abstract base class for all integration providers (e.g., Google, HubSpot).
    Defines the contract for OAuth flow and API interactions.
    """

    def __init__(self, integration_type: IntegrationType):
        self._integration_type = integration_type

    @property
    def integration_type(self) -> IntegrationType:
        """The IntegrationType this provider handles."""
        return self._integration_type

    @property
    @abstractmethod
    def provider_key(self) -> str:
        """Stable provider identifier shared across related integration types."""
        ...

    @abstractmethod
    async def get_authorization_url(self, state: str) -> str:
        """
        Generate the OAuth authorization URL.

        Args:
            state: Secure state string (typically a JWT) containing user/org context.

        Returns:
            The full URL to redirect the user to.
        """
        ...

    @abstractmethod
    async def exchange_code(self, code: str, redirect_uri: str | None = None) -> dict[str, Any]:
        """
        Exchange the authorization code for access/refresh tokens.

        Args:
            code: The OAuth authorization code.
            redirect_uri: The URI the provider redirected to.

        Returns:
            Dictionary containing tokens (`access_token`, `refresh_token`, `expires_in`).
        """
        ...

    @abstractmethod
    async def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        """
        Refresh an expired access token using the refresh token.

        Args:
            refresh_token: The stored refresh token.

        Returns:
            Dictionary containing new tokens (`access_token`, `expires_in`, and optionally a new `refresh_token`).
        """
        ...

    @abstractmethod
    async def revoke_token(self, token: str) -> None:
        """
        Revoke an OAuth token at the provider so the connection is actually disconnected.

        Args:
            token: Refresh token preferred, otherwise access token.
        """
        ...

    @abstractmethod
    async def get_user_info(self, access_token: str) -> dict[str, Any]:
        """
        Fetch the authenticated user's basic profile details (e.g., email) from the provider.

        Args:
            access_token: The valid OAuth access token.

        Returns:
            Dictionary with user details (must include `email` if available).
        """
        ...
