"""
Nango REST API client for managing OAuth connections and API interactions.

This module provides a clean interface to Nango's REST API for:
- Initiating OAuth flows
- Exchanging authorization codes for tokens
- Refreshing expired tokens
- Managing connections
- Making authenticated API calls to integrated services

Note: Nango doesn't have an official Python SDK, so we use their REST API directly.
API Documentation: https://docs.nango.dev/reference/api/connection/get
"""

from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta, timezone
import logging
import httpx

from src.infra.config.configs import nango_integration_config

logger = logging.getLogger(__name__)


class NangoConnectionError(Exception):
    """Raised when there's an error with Nango connection operations."""

    pass


class NangoTokenError(Exception):
    """Raised when there's an error with token operations."""

    pass


class NangoClient:
    """
    REST API client for Nango OAuth connection management.

    This client handles all interactions with Nango's REST API including:
    - OAuth authorization URL generation
    - Token exchange and refresh
    - Connection management
    - Authenticated API requests to integrated services

    API Reference: https://docs.nango.dev/reference/api
    """

    def __init__(self, secret_key: Optional[str] = None):
        """
        Initialize the Nango REST API client.

        Args:
            secret_key: Nango secret key. If not provided, uses config value

        Raises:
            ValueError: If secret key is missing
        """
        self.secret_key = secret_key or nango_integration_config.NANGO_SECRET_KEY
        if not self.secret_key:
            raise ValueError("NANGO_SECRET_KEY must be set in environment variables")

        self.base_url = nango_integration_config.NANGO_BASE_URL

        # Create httpx client with default headers
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.secret_key}",
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        logger.info(f"Initialized Nango REST API client with base URL: {self.base_url}")

    async def create_connect_session(
        self,
        end_user_id: str,
        organization_id: str,
        integration_id: str,
        end_user_email: Optional[str] = None,
        end_user_display_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create a Nango Connect Session for OAuth authorization.

        New flow (post July 31, 2025): Instead of generating URLs with public keys,
        we create a session token that the frontend uses with Nango Connect UI.

        API: POST /connect/sessions
        Docs: https://docs.nango.dev/reference/api/connect/sessions/create

        Args:
            end_user_id: Your internal user ID
            organization_id: Your internal organization ID
            integration_id: The integration/provider config key (e.g., 'hubspot', 'slack')
            end_user_email: User's email (optional, for display)
            end_user_display_name: User's name (optional, for display)

        Returns:
            Dictionary containing:
                - token: Session token to send to frontend
                - expires_at: When the token expires
                - end_user_id: The user ID

        Raises:
            NangoConnectionError: If session creation fails
        """
        try:
            payload = {
                "end_user": {
                    "id": end_user_id,
                    "email": end_user_email,
                    "display_name": end_user_display_name,
                    "tags": {"organizationId": organization_id},
                },
                "allowed_integrations": [integration_id],
            }

            response = await self.client.post("/connect/sessions", json=payload)
            response.raise_for_status()

            result = response.json()

            logger.info(
                f"Created Connect Session for user={end_user_id}, "
                f"org={organization_id}, integration={integration_id}"
            )

            return result

        except httpx.HTTPStatusError as e:
            logger.error(
                f"HTTP error creating connect session: {e.response.status_code} - {e.response.text}"
            )
            raise NangoConnectionError(f"Failed to create connect session: {str(e)}")
        except Exception as e:
            logger.error(f"Failed to create connect session: {str(e)}")
            raise NangoConnectionError(f"Failed to create connect session: {str(e)}")

    async def exchange_code_for_token(
        self,
        provider: str,
        connection_id: str,
        code: str,
    ) -> Dict[str, Any]:
        """
        Exchange authorization code for access token.

        Note: Nango typically handles this automatically via their OAuth callback.
        This method is provided for completeness but may not be needed in most cases.

        Args:
            provider: Integration provider
            connection_id: Connection identifier
            code: Authorization code from OAuth callback

        Returns:
            Dictionary containing connection details including tokens

        Raises:
            NangoTokenError: If token exchange fails
        """
        try:
            # Nango handles token exchange automatically, so we just verify the connection
            connection = await self.get_connection(provider, connection_id)

            logger.info(f"Successfully verified connection for provider={provider}")
            return connection

        except Exception as e:
            logger.error(f"Failed to verify connection after code exchange: {str(e)}")
            raise NangoTokenError(f"Failed to exchange code: {str(e)}")

    async def get_connection(
        self,
        provider: str,
        connection_id: str,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        """
        Retrieve connection details including token status.

        API: GET /connection/{connection_id}
        Docs: https://docs.nango.dev/reference/api/connection/get

        Args:
            provider: Integration provider
            connection_id: Connection identifier
            force_refresh: If True, forces a token refresh before returning

        Returns:
            Dictionary containing:
                - connection_id: The connection identifier
                - provider_config_key: Provider name
                - credentials: Token information
                - connection_config: Additional configuration
                - created_at: When connection was created
                - metadata: Additional metadata about the connection

        Raises:
            NangoConnectionError: If connection retrieval fails
        """
        try:
            params = {
                "provider_config_key": provider,
                "force_refresh": "true" if force_refresh else "false",
            }

            url = f"/connection/{connection_id}"
            response = await self.client.get(url, params=params)
            response.raise_for_status()

            connection = response.json()

            logger.info(
                f"Retrieved connection for provider={provider}, id={connection_id}"
            )
            return connection

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise NangoConnectionError(f"Connection not found: {connection_id}")
            logger.error(
                f"HTTP error getting connection: {e.response.status_code} - {e.response.text}"
            )
            raise NangoConnectionError(f"Failed to get connection: {str(e)}")
        except Exception as e:
            logger.error(f"Failed to get connection: {str(e)}")
            raise NangoConnectionError(f"Failed to get connection: {str(e)}")

    async def refresh_token(
        self,
        provider: str,
        connection_id: str,
    ) -> Dict[str, Any]:
        """
        Refresh an expired access token.

        API: Uses GET /connection with force_refresh=true
        Docs: https://docs.nango.dev/reference/api/connection/get

        Args:
            provider: Integration provider
            connection_id: Connection identifier

        Returns:
            Dictionary with updated connection details including new tokens

        Raises:
            NangoTokenError: If token refresh fails
        """
        try:
            # Force refresh the connection
            connection = await self.get_connection(
                provider=provider,
                connection_id=connection_id,
                force_refresh=True,
            )

            logger.info(f"Successfully refreshed token for provider={provider}")
            return connection

        except Exception as e:
            logger.error(f"Failed to refresh token: {str(e)}")
            raise NangoTokenError(f"Failed to refresh token: {str(e)}")

    async def delete_connection(
        self,
        provider: str,
        connection_id: str,
    ) -> None:
        """
        Delete a connection and revoke tokens.

        API: DELETE /connection/{connection_id}
        Docs: https://docs.nango.dev/reference/api/connection/delete

        Args:
            provider: Integration provider
            connection_id: Connection identifier

        Raises:
            NangoConnectionError: If deletion fails
        """
        try:
            params = {"provider_config_key": provider}
            url = f"/connection/{connection_id}"

            response = await self.client.delete(url, params=params)
            response.raise_for_status()

            logger.info(
                f"Deleted connection for provider={provider}, id={connection_id}"
            )

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.warning(
                    f"Connection not found (already deleted?): {connection_id}"
                )
                return  # Consider it success if already gone
            logger.error(
                f"HTTP error deleting connection: {e.response.status_code} - {e.response.text}"
            )
            raise NangoConnectionError(f"Failed to delete connection: {str(e)}")
        except Exception as e:
            logger.error(f"Failed to delete connection: {str(e)}")
            raise NangoConnectionError(f"Failed to delete connection: {str(e)}")

    async def get_connection_metadata(
        self,
        provider: str,
        connection_id: str,
    ) -> Dict[str, Any]:
        """
        Get metadata about the connected account (e.g., user info, account details).

        Args:
            provider: Integration provider
            connection_id: Connection identifier

        Returns:
            Dictionary containing account metadata

        Raises:
            NangoConnectionError: If metadata retrieval fails
        """
        try:
            connection = await self.get_connection(provider, connection_id)
            return connection.get("metadata", {})

        except Exception as e:
            logger.error(f"Failed to get connection metadata: {str(e)}")
            raise NangoConnectionError(f"Failed to get metadata: {str(e)}")

    async def proxy_request(
        self,
        provider: str,
        connection_id: str,
        endpoint: str,
        method: str = "GET",
        data: Optional[Dict] = None,
        params: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """
        Make an authenticated API request to the integrated service via Nango proxy.

        This allows you to call the provider's API using Nango's proxy,
        which automatically handles authentication and token refresh.

        API: POST /proxy
        Docs: https://docs.nango.dev/reference/api/proxy/post

        Args:
            provider: Integration provider
            connection_id: Connection identifier
            endpoint: API endpoint to call (e.g., '/v1/contacts')
            method: HTTP method (GET, POST, PUT, DELETE, etc.)
            data: Request body for POST/PUT requests
            params: Query parameters

        Returns:
            API response data

        Raises:
            NangoConnectionError: If API request fails
        """
        try:
            # Nango proxy endpoint
            url = "/proxy"

            # Build proxy request payload
            payload: Dict[str, Any] = {
                "provider_config_key": provider,
                "connection_id": connection_id,
                "endpoint": endpoint,
                "method": method.upper(),
            }

            if params:
                payload["params"] = params
            if data:
                payload["data"] = data

            response = await self.client.post(url, json=payload)
            response.raise_for_status()

            result = response.json()

            logger.info(
                f"Proxy request to {provider}:{endpoint} "
                f"(method={method}) succeeded"
            )
            return result

        except httpx.HTTPStatusError as e:
            logger.error(
                f"HTTP error in proxy request: {e.response.status_code} - {e.response.text}"
            )
            raise NangoConnectionError(f"Proxy request failed: {str(e)}")
        except Exception as e:
            logger.error(f"Proxy request failed: {str(e)}")
            raise NangoConnectionError(f"Proxy request failed: {str(e)}")

    def parse_token_expiry(
        self, expires_in: Optional[int] = None
    ) -> Optional[datetime]:
        """
        Parse token expiry time.

        Args:
            expires_in: Number of seconds until token expires

        Returns:
            DateTime when token expires, or None if no expiry
        """
        if expires_in is None:
            return None

        return datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    def is_token_expiring_soon(
        self,
        expires_at: Optional[datetime],
        buffer_minutes: Optional[int] = None,
    ) -> bool:
        """
        Check if a token is expiring soon.

        Args:
            expires_at: When the token expires
            buffer_minutes: How many minutes before expiry to consider "soon"
                          (defaults to INTEGRATION_TOKEN_REFRESH_BUFFER_MINUTES)

        Returns:
            True if token is expiring within the buffer period
        """
        if expires_at is None:
            return False

        buffer = (
            buffer_minutes
            or nango_integration_config.INTEGRATION_TOKEN_REFRESH_BUFFER_MINUTES
        )
        threshold = datetime.now(timezone.utc) + timedelta(minutes=buffer)

        return expires_at <= threshold

    async def close(self):
        """Close the httpx client connection."""
        await self.client.aclose()
        logger.info("Closed Nango REST API client")
