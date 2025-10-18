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

import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from src.infra.config.configs import nango_integration_config

logger = logging.getLogger(__name__)


class NangoConnectionError(Exception):
    """Raised when there's an error with Nango connection operations."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


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

    def __init__(self, secret_key: str | None = None):
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

    @staticmethod
    def _lookup_error_message(payload: Any) -> str | None:
        """
        Extract a human-readable error message from a Nango error payload.
        """
        if isinstance(payload, dict):
            # Nango typically nests details under "error" and/or "errors"
            error_block = payload.get("error") or payload
            if isinstance(error_block, dict):
                message = error_block.get("message")
                if isinstance(message, str) and message.strip():
                    return message.strip()

                errors = error_block.get("errors")
                if isinstance(errors, list) and errors:
                    first = errors[0]
                    if isinstance(first, dict):
                        nested_message = first.get("message")
                        if isinstance(nested_message, str) and nested_message.strip():
                            return nested_message.strip()
                    # Fallback: string representation of first error entry
                    return str(first)

            # Fallback to top-level message field if present
            message = payload.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()

        if isinstance(payload, list) and payload:
            first = payload[0]
            if isinstance(first, dict):
                nested_message = first.get("message")
                if isinstance(nested_message, str) and nested_message.strip():
                    return nested_message.strip()
            return str(first)

        if isinstance(payload, str):
            stripped = payload.strip()
            return stripped or None

        return None

    @staticmethod
    def _extract_error_details(
        response: httpx.Response | None,
    ) -> tuple[int | None, Any, str | None]:
        """
        Extract status code, payload, and message from an httpx response object.
        """
        if response is None:
            return None, None, None

        status_code = response.status_code
        try:
            payload: Any = response.json()
        except (ValueError, json.JSONDecodeError):
            payload = response.text

        message = NangoClient._lookup_error_message(payload)
        return status_code, payload, message

    @staticmethod
    def verify_webhook_signature(
        signature: str, payload: dict[str, Any], webhook_secret: str | None = None
    ) -> bool:
        """
        Verify the X-Nango-Signature header from a webhook request.

        Nango signs webhook payloads using SHA-256 hash (NOT HMAC).
        The signature is the hex digest of: SHA256(secret_key + JSON_payload)

        Docs: https://docs.nango.dev/implementation-guides/platform/webhooks-from-nango#verifying-webhooks-from-nango
        Source: https://github.com/NangoHQ/nango/blob/main/packages/node-client/lib/index.ts#L1037

        Args:
            signature: The X-Nango-Signature header value
            payload: The webhook request body as a dictionary
            webhook_secret: The secret key from Nango (NANGO_SECRET_KEY).
                          If not provided, uses config value.

        Returns:
            True if signature is valid, False otherwise

        Example:
            signature = request.headers.get("X-Nango-Signature")
            is_valid = NangoClient.verify_webhook_signature(signature, request.json())
        """
        secret = webhook_secret or nango_integration_config.NANGO_SECRET_KEY
        if not secret:
            logger.error("NANGO_SECRET_KEY not configured - cannot verify webhook")
            return False

        if not signature:
            logger.warning("No X-Nango-Signature header provided")
            return False

        try:
            # Convert payload to JSON string (same format Nango uses)
            # Nango uses JSON.stringify() which doesn't sort keys
            payload_json = json.dumps(payload, separators=(",", ":"))

            # Compute SHA-256 hash (NOT HMAC!)
            # Formula: SHA256(secret + payload_json)
            signature_input = f"{secret}{payload_json}"
            expected_signature = hashlib.sha256(signature_input.encode("utf-8")).hexdigest()

            # Constant-time comparison to prevent timing attacks
            is_valid = hmac.compare_digest(signature, expected_signature)

            if not is_valid:
                logger.warning(
                    f"Webhook signature mismatch. Expected: {expected_signature[:10]}..., "
                    f"Got: {signature[:10]}..."
                )

            return is_valid

        except Exception as e:
            logger.error(f"Error verifying webhook signature: {str(e)}")
            return False

    async def create_connect_session(
        self,
        end_user_id: str,
        organization_id: str,
        integration_id: str,
        end_user_email: str | None = None,
        end_user_display_name: str | None = None,
    ) -> dict[str, Any]:
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
            # Build end_user object without null fields (Nango rejects null strings)
            end_user: dict[str, Any] = {
                "id": end_user_id,
                "tags": {"organizationId": organization_id},
            }
            if end_user_email is not None:
                end_user["email"] = end_user_email
            if end_user_display_name is not None:
                end_user["display_name"] = end_user_display_name

            payload = {
                "end_user": end_user,
                "allowed_integrations": [integration_id],
            }

            # Debug: high-level payload details (no secrets)
            logger.debug(
                "Creating Nango Connect session: end_user_id=%s org_id=%s integration=%s has_email=%s has_display_name=%s",
                end_user_id,
                organization_id,
                integration_id,
                str(end_user_email is not None),
                str(end_user_display_name is not None),
            )

            response = await self.client.post("/connect/sessions", json=payload)
            response.raise_for_status()

            result = response.json()

            # Normalize response to expose token and expires_at at top-level
            if isinstance(result, dict):
                envelope = result.get("data") if isinstance(result.get("data"), dict) else None
                body = envelope if envelope is not None else result
                token = (
                    body.get("token")
                    or body.get("session_token")
                    or body.get("connect_token")
                    or (body.get("session") or {}).get("token")
                )
                expires_at = body.get("expires_at") or (body.get("session") or {}).get("expires_at")
                # Attach normalized fields for consumers
                result["token"] = token
                result["expires_at"] = expires_at

                # Debug with masked token
                masked = (
                    f"{str(token)[:6]}...{str(token)[-4:]}"
                    if isinstance(token, str) and len(token) > 12
                    else "***masked***"
                )
                logger.debug(
                    "Nango Connect session response normalized: has_envelope=%s token=%s expires_at=%s",
                    str(envelope is not None),
                    masked,
                    expires_at,
                )

            logger.info(
                f"Created Connect Session for user={end_user_id}, "
                f"org={organization_id}, integration={integration_id}"
            )

            return result

        except httpx.HTTPStatusError as e:
            status_code, payload, detail = self._extract_error_details(e.response)
            context = {
                "end_user_id": end_user_id,
                "organization_id": organization_id,
                "integration_id": integration_id,
            }
            logger.error(
                "HTTP error creating connect session (status=%s, context=%s): %s",
                status_code,
                context,
                payload,
            )
            message = detail or f"Failed to create connect session: {str(e)}"
            raise NangoConnectionError(
                message,
                status_code=status_code,
                payload=payload,
            ) from None
        except Exception as e:
            logger.error(
                "Failed to create connect session for context=%s: %s",
                {
                    "end_user_id": end_user_id,
                    "organization_id": organization_id,
                    "integration_id": integration_id,
                },
                str(e),
            )
            raise NangoConnectionError(f"Failed to create connect session: {str(e)}") from None

    async def exchange_code_for_token(
        self,
        provider: str,
        connection_id: str,
        code: str,
    ) -> dict[str, Any]:
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
    ) -> dict[str, Any]:
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

            logger.info(f"Retrieved connection for provider={provider}, id={connection_id}")
            return connection

        except httpx.HTTPStatusError as e:
            status_code, payload, detail = self._extract_error_details(e.response)
            if status_code == 404:
                raise NangoConnectionError(
                    f"Connection not found: {connection_id}",
                    status_code=status_code,
                    payload=payload,
                ) from None
            logger.error(
                "HTTP error retrieving connection (status=%s, provider=%s, connection_id=%s): %s",
                status_code,
                provider,
                connection_id,
                payload,
            )
            message = detail or f"Failed to get connection: {str(e)}"
            raise NangoConnectionError(
                message,
                status_code=status_code,
                payload=payload,
            ) from None
        except Exception as e:
            logger.error(f"Failed to get connection: {str(e)}")
            raise NangoConnectionError(f"Failed to get connection: {str(e)}") from None

    async def refresh_token(
        self,
        provider: str,
        connection_id: str,
    ) -> dict[str, Any]:
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

            logger.info(f"Deleted connection for provider={provider}, id={connection_id}")

        except httpx.HTTPStatusError as e:
            status_code, payload, detail = self._extract_error_details(e.response)
            if status_code == 404:
                logger.warning(
                    "Connection not found in Nango when deleting (provider=%s, connection_id=%s)",
                    provider,
                    connection_id,
                )
                return  # Consider it success if already gone
            logger.error(
                "HTTP error deleting connection (status=%s, provider=%s, connection_id=%s): %s",
                status_code,
                provider,
                connection_id,
                payload,
            )
            message = detail or f"Failed to delete connection: {str(e)}"
            raise NangoConnectionError(
                message,
                status_code=status_code,
                payload=payload,
            ) from None
        except Exception as e:
            logger.error(f"Failed to delete connection: {str(e)}")
            raise NangoConnectionError(f"Failed to delete connection: {str(e)}") from None

    async def get_connection_metadata(
        self,
        provider: str,
        connection_id: str,
    ) -> dict[str, Any]:
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
        data: dict | None = None,
        params: dict | None = None,
    ) -> dict[str, Any]:
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
            payload: dict[str, Any] = {
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

            logger.info(f"Proxy request to {provider}:{endpoint} " f"(method={method}) succeeded")
            return result

        except httpx.HTTPStatusError as e:
            status_code, error_payload, detail = self._extract_error_details(e.response)
            logger.error(
                "HTTP error in proxy request (status=%s, provider=%s, endpoint=%s, method=%s): %s",
                status_code,
                provider,
                endpoint,
                method.upper(),
                error_payload,
            )
            message = detail or f"Proxy request failed: {str(e)}"
            raise NangoConnectionError(
                message,
                status_code=status_code,
                payload=error_payload,
            ) from None
        except Exception as e:
            logger.error(f"Proxy request failed: {str(e)}")
            raise NangoConnectionError(f"Proxy request failed: {str(e)}") from None

    def parse_token_expiry(self, expires_in: int | None = None) -> datetime | None:
        """
        Parse token expiry time.

        Args:
            expires_in: Number of seconds until token expires

        Returns:
            DateTime when token expires, or None if no expiry
        """
        if expires_in is None:
            return None

        return datetime.now(UTC) + timedelta(seconds=expires_in)

    def is_token_expiring_soon(
        self,
        expires_at: datetime | None,
        buffer_minutes: int | None = None,
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

        buffer = buffer_minutes or nango_integration_config.INTEGRATION_TOKEN_REFRESH_BUFFER_MINUTES
        threshold = datetime.now(UTC) + timedelta(minutes=buffer)

        return expires_at <= threshold

    async def close(self):
        """Close the httpx client connection."""
        await self.client.aclose()
        logger.info("Closed Nango REST API client")
