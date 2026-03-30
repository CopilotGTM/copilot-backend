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
from typing import Any, NoReturn, cast

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
        logger.debug(f"Initialized Nango REST API client with base URL: {self.base_url}")

    @staticmethod
    def _lookup_error_message(payload: Any) -> str | None:
        """
        Extract a human-readable error message from a Nango error payload.
        """
        if isinstance(payload, dict):
            return NangoClient._extract_message_from_dict(payload)

        if isinstance(payload, list) and payload:
            return NangoClient._extract_message_from_list(payload)

        if isinstance(payload, str):
            stripped = payload.strip()
            return stripped or None

        return None

    @staticmethod
    def _extract_message_from_dict(payload: dict) -> str | None:
        """Extract message from dictionary payload."""
        # Try error block first
        error_block = payload.get("error") or payload
        if isinstance(error_block, dict):
            message = NangoClient._get_string_message(error_block.get("message"))
            if message:
                return message

            # Check errors array
            message = NangoClient._extract_from_errors_array(error_block.get("errors"))
            if message:
                return message

        # Fallback to top-level message
        return NangoClient._get_string_message(payload.get("message"))

    @staticmethod
    def _extract_from_errors_array(errors: Any) -> str | None:
        """Extract message from errors array."""
        if not isinstance(errors, list) or not errors:
            return None

        first = errors[0]
        if isinstance(first, dict):
            message = NangoClient._get_string_message(first.get("message"))
            if message:
                return message

        return str(first)

    @staticmethod
    def _extract_message_from_list(payload: list) -> str | None:
        """Extract message from list payload."""
        first = payload[0]
        if isinstance(first, dict):
            message = NangoClient._get_string_message(first.get("message"))
            if message:
                return message
        return str(first)

    @staticmethod
    def _get_string_message(value: Any) -> str | None:
        """Get string message if value is non-empty string."""
        if isinstance(value, str) and value.strip():
            return value.strip()
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
        except json.JSONDecodeError:
            payload = response.text

        message = NangoClient._lookup_error_message(payload)
        return status_code, payload, message

    @staticmethod
    def _event_extra(event: str, **fields: Any) -> dict[str, Any]:
        return {"event": event, **{key: value for key, value in fields.items() if value is not None}}

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
            payload = self._build_connect_session_payload(
                end_user_id, organization_id, integration_id, end_user_email, end_user_display_name
            )

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

            raw = response.json()
            result = self._normalize_connect_session_response(cast(dict[str, Any], raw))

            logger.debug(
                "Connect session created",
                extra=self._event_extra(
                    "integration_connect_session_created",
                    organisation_id=organization_id,
                    user_id=end_user_id,
                    integration_id=integration_id,
                ),
            )

            return result

        except httpx.HTTPStatusError as e:
            self._handle_connect_session_http_error(e, end_user_id, organization_id, integration_id)
        except Exception as e:
            self._handle_connect_session_error(e, end_user_id, organization_id, integration_id)

    def _build_connect_session_payload(
        self,
        end_user_id: str,
        organization_id: str,
        integration_id: str,
        end_user_email: str | None,
        end_user_display_name: str | None,
    ) -> dict[str, Any]:
        """Build payload for connect session creation (excluding null fields)."""
        end_user: dict[str, Any] = {
            "id": end_user_id,
            "tags": {"organizationId": organization_id},
        }
        if end_user_email is not None:
            end_user["email"] = end_user_email
        if end_user_display_name is not None:
            end_user["display_name"] = end_user_display_name

        return {
            "end_user": end_user,
            "allowed_integrations": [integration_id],
        }

    def _normalize_connect_session_response(self, result: dict[str, Any]) -> dict[str, Any]:
        """Normalize Nango Connect session response to expose token and expires_at at top-level."""
        if not isinstance(result, dict):
            return result

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

        return result

    def _handle_connect_session_http_error(
        self, e: httpx.HTTPStatusError, end_user_id: str, organization_id: str, integration_id: str
    ) -> NoReturn:
        """Handle HTTP errors during connect session creation."""
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

    def _handle_connect_session_error(
        self, e: Exception, end_user_id: str, organization_id: str, integration_id: str
    ) -> NoReturn:
        """Handle general errors during connect session creation."""
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

            logger.debug(f"Successfully verified connection for provider={provider}")
            return connection

        except Exception as e:
            logger.error(f"Failed to verify connection after code exchange: {str(e)}")
            raise NangoTokenError(f"Failed to exchange code: {str(e)}") from e

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

            connection: dict[str, Any] = cast(dict[str, Any], response.json())

            logger.debug(
                "Integration connection retrieved",
                extra=self._event_extra(
                    "integration_connection_retrieved",
                    provider_config_key=provider,
                    connection_id=connection_id,
                ),
            )
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

            logger.debug(
                "Integration token refreshed",
                extra=self._event_extra(
                    "integration_token_refreshed",
                    provider_config_key=provider,
                    connection_id=connection_id,
                ),
            )
            return connection

        except Exception as e:
            logger.error(
                "Integration token refresh failed",
                extra=self._event_extra(
                    "integration_token_refresh_failed",
                    provider_config_key=provider,
                    connection_id=connection_id,
                    failure_reason=str(e),
                ),
            )
            raise NangoTokenError(f"Failed to refresh token: {str(e)}") from e

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

            logger.debug(
                "Integration connection deleted",
                extra=self._event_extra(
                    "integration_connection_deleted",
                    provider_config_key=provider,
                    connection_id=connection_id,
                ),
            )

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
            metadata: dict[str, Any] = cast(dict[str, Any], connection.get("metadata", {}))
            return metadata

        except Exception as e:
            logger.error(f"Failed to get connection metadata: {str(e)}")
            raise NangoConnectionError(f"Failed to get metadata: {str(e)}") from e

    async def action(
        self,
        provider_config_key: str,
        connection_id: str,
        action_name: str,
        input_data: dict | None = None,
    ) -> dict[str, Any]:
        """
        Trigger a Nango action (workflow).

        Actions are server-side workflows that can fetch data or perform operations
        that don't fit the sync model (e.g., one-time data fetches, complex operations).

        API: POST /action/trigger
        Docs: https://docs.nango.dev/integrate/guides/perform-workflows-with-an-api

        Args:
            provider_config_key: Nango provider config key (e.g., 'hubspot')
            connection_id: Connection identifier
            action_name: Name of the action to trigger (e.g., 'fetch-pipelines')
            input_data: Optional input data for the action

        Returns:
            Action result data

        Raises:
            NangoConnectionError: If action fails

        Example:
            result = await nango.action(
                provider_config_key="hubspot",
                connection_id="org_123",
                action_name="fetch-pipelines"
            )
        """
        try:
            # Build headers (Nango requires these as headers, not body)
            headers = {
                "Provider-Config-Key": provider_config_key,
                "Connection-Id": connection_id,
            }

            # Build request payload
            payload: dict[str, Any] = {
                "action_name": action_name,
            }
            if input_data:
                payload["input"] = input_data

            logger.debug(
                "Triggering Nango action: provider=%s, connection=%s, action=%s",
                provider_config_key,
                connection_id,
                action_name,
            )

            response = await self.client.post("/action/trigger", json=payload, headers=headers)
            response.raise_for_status()

            result: dict[str, Any] = cast(dict[str, Any], response.json())

            logger.debug(
                "Integration action completed",
                extra=self._event_extra(
                    "integration_action_completed",
                    provider_config_key=provider_config_key,
                    connection_id=connection_id,
                    action_name=action_name,
                ),
            )
            return result

        except httpx.HTTPStatusError as e:
            status_code, error_payload, detail = self._extract_error_details(e.response)
            logger.error(
                "HTTP error triggering action (status=%s, provider=%s, action=%s): %s",
                status_code,
                provider_config_key,
                action_name,
                error_payload,
            )
            message = detail or f"Action {action_name} failed: {str(e)}"
            raise NangoConnectionError(
                message,
                status_code=status_code,
                payload=error_payload,
            ) from None
        except Exception as e:
            logger.error(
                "Integration action failed",
                extra=self._event_extra(
                    "integration_action_failed",
                    provider_config_key=provider_config_key,
                    connection_id=connection_id,
                    action_name=action_name,
                    failure_reason=str(e),
                ),
            )
            raise NangoConnectionError(f"Action {action_name} failed: {str(e)}") from None

    async def proxy_request(
        self,
        provider_config_key: str,
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
            provider_config_key: Nango provider config key (e.g., 'google-calendar')
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
            # Nango proxy endpoint - path is proxied as-is to provider API
            # Format: /proxy{endpoint}
            # Docs: https://docs.nango.dev/reference/api/proxy/get
            url = f"/proxy{endpoint}"

            # Only pass through actual provider query params
            proxy_params = dict(params or {})

            # Required Nango headers (case-insensitive but use canonical casing)
            headers = {
                "Provider-Config-Key": provider_config_key,
                "Connection-Id": connection_id,
            }

            # Make the request using the specified HTTP method
            if method.upper() == "GET":
                response = await self.client.get(url, params=proxy_params, headers=headers)
            elif method.upper() == "POST":
                response = await self.client.post(
                    url, params=proxy_params, json=data or {}, headers=headers
                )
            elif method.upper() == "PUT":
                response = await self.client.put(
                    url, params=proxy_params, json=data or {}, headers=headers
                )
            elif method.upper() == "DELETE":
                response = await self.client.delete(url, params=proxy_params, headers=headers)
            elif method.upper() == "PATCH":
                response = await self.client.patch(
                    url, params=proxy_params, json=data or {}, headers=headers
                )
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")
            response.raise_for_status()

            result: dict[str, Any] = cast(dict[str, Any], response.json())

            logger.debug(
                "Integration proxy request succeeded",
                extra=self._event_extra(
                    "integration_proxy_request_succeeded",
                    provider_config_key=provider_config_key,
                    connection_id=connection_id,
                    endpoint=endpoint,
                    http_method=method.upper(),
                ),
            )
            return result

        except httpx.HTTPStatusError as e:
            status_code, error_payload, detail = self._extract_error_details(e.response)
            logger.error(
                "HTTP error in proxy request (status=%s, provider=%s, endpoint=%s, method=%s): %s",
                status_code,
                provider_config_key,
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
            logger.error(
                "Integration proxy request failed",
                extra=self._event_extra(
                    "integration_proxy_request_failed",
                    provider_config_key=provider_config_key,
                    connection_id=connection_id,
                    endpoint=endpoint,
                    http_method=method.upper(),
                    failure_reason=str(e),
                ),
            )
            raise NangoConnectionError(f"Proxy request failed: {str(e)}") from None

    def parse_token_expiry(self, expires_value: int | str | None = None) -> datetime | None:
        """
        Parse token expiry time.

        Args:
            expires_value: Either:
                - Number of seconds until token expires (int or string of int)
                - ISO datetime string (e.g., '2026-01-18T13:09:12.606Z')

        Returns:
            DateTime when token expires (naive UTC), or None if no expiry
        """
        if expires_value is None:
            return None

        # If it's a string, check if it's an ISO datetime or seconds
        if isinstance(expires_value, str):
            # ISO datetime string (contains 'T')
            if "T" in expires_value:
                # Parse ISO datetime and return as naive UTC
                # Handle 'Z' suffix (UTC indicator)
                iso_str = expires_value.replace("Z", "+00:00")
                parsed = datetime.fromisoformat(iso_str)
                # Return naive datetime (remove timezone for DB compatibility)
                return parsed.replace(tzinfo=None)
            else:
                # It's a string of seconds
                return datetime.utcnow() + timedelta(seconds=int(expires_value))

        # It's an int (seconds)
        return datetime.utcnow() + timedelta(seconds=expires_value)

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
        logger.debug("Closed Nango REST API client")



