"""
OAuth flow routes for integration management.

Handles:
- Initiating OAuth flows via Nango Connect UI
- Webhook processing from Nango after authorization
- Connection setup and validation
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.master_dependencies import acquire_master_async_session
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    acquire_org_async_session,
    get_current_user,
    get_org_async_session,
)
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.integrations import (
    OAuthInitiateRequest,
    OAuthInitiateResponse,
)
from src.api.utils.http_response import Response
from src.domain.models.master_tables import User, UserAppRole
from src.domain.models.organisation_tables.knowledge_models import (
    IntegrationScope,
    IntegrationType,
)
from src.infra.auth.user_auth import user_auth
from src.infra.config.constant import IntegrationErrors
from src.infra.config.integrations_metadata import (
    AvailabilityStatus,
    IntegrationMetadata,
    get_integration_metadata,
)
from src.infra.config.nango_provider_mapping import (
    from_nango_provider_key,
    to_nango_connect_integration_key,
    to_nango_provider_key,
)
from src.infra.utils.logger import get_logger
from src.service.integrations import IntegrationService, NangoClient
from src.service.integrations.nango_client import NangoConnectionError

logger = get_logger(__name__)
router = APIRouter(prefix="/oauth", tags=["integrations-oauth"])

_ALLOWED_AVAILABILITY_STATUSES = {
    AvailabilityStatus.AVAILABLE,
    AvailabilityStatus.BETA,
}


class IntegrationUnavailableError(Exception):
    """Raised when an integration is not ready for user connections."""

    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


def _extract_string_message(value: Any) -> str | None:
    """Extract and return a stripped string message if valid."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _extract_message_from_dict(data: dict) -> str | None:
    """Extract message from a dictionary, checking 'message' key."""
    message = data.get("message")
    return _extract_string_message(message)


def _extract_message_from_error_list(errors: list) -> str | None:
    """Extract first valid message from a list of error entries."""
    for entry in errors:
        if isinstance(entry, dict):
            message = _extract_message_from_dict(entry)
            if message:
                return message
        else:
            message = _extract_string_message(entry)
            if message:
                return message
    return None


def _extract_message_from_error_block(error_block: dict) -> str | None:
    """Extract message from error block, checking message and errors array."""
    # Try direct message first
    message = _extract_message_from_dict(error_block)
    if message:
        return message

    # Try errors array
    errors = error_block.get("errors")
    if isinstance(errors, list):
        return _extract_message_from_error_list(errors)

    return None


def _extract_nango_error_message(payload: Any) -> str | None:
    """
    Pull a user-friendly message from a Nango error payload.
    Handles dict, list, and string payloads with nested error structures.
    """
    # Handle dictionary payload
    if isinstance(payload, dict):
        # Check error block first
        error_block = payload.get("error")
        if isinstance(error_block, dict):
            message = _extract_message_from_error_block(error_block)
            if message:
                return message

        # Fall back to payload itself as error block
        message = _extract_message_from_error_block(payload)
        if message:
            return message

    # Handle list payload
    if isinstance(payload, list):
        return _extract_message_from_error_list(payload)

    # Handle string payload
    return _extract_string_message(payload)


def _get_validated_metadata(
    integration_type: IntegrationType,
) -> IntegrationMetadata:
    metadata = get_integration_metadata(integration_type)
    if not metadata:
        raise ValueError(f"Unknown integration type: {integration_type}")
    return metadata


def _ensure_integration_available(metadata: IntegrationMetadata) -> None:
    if metadata.availability_status not in _ALLOWED_AVAILABILITY_STATUSES:
        raise IntegrationUnavailableError(metadata.availability_status)


def _should_include_stacktrace(error: NangoConnectionError) -> bool:
    if error.status_code is None:
        return True
    return isinstance(error.status_code, int) and error.status_code >= 500


def _extract_session_details(session_data: dict[str, Any]) -> tuple[str, str | None]:
    raw = session_data.get("data") if isinstance(session_data, dict) else None
    payload = raw if isinstance(raw, dict) else session_data

    token = payload.get("token") or (payload.get("session") or {}).get("token")
    expires_at = payload.get("expires_at") or (payload.get("session") or {}).get("expires_at")

    if not token:
        logger.error(f"Nango connect session created but token missing. Payload: {session_data}")
        raise ValueError("Failed to retrieve session token")

    return token, expires_at


async def _create_nango_connect_session(
    nango: NangoClient,
    user_ids: UserIDs,
    user: User,
    integration_key: str,
    provider_key: str,
    integration_type: IntegrationType,
) -> dict[str, Any]:
    session_data = await nango.create_connect_session(
        end_user_id=str(user_ids.user_id),
        organization_id=str(user_ids.organisation_id),
        integration_id=integration_key,
        end_user_email=user.email,
        end_user_display_name=f"{user.first_name} {user.last_name}",
    )

    logger.info(f"Nango Connect session data: {session_data}")
    logger.info(
        "Created Connect Session for user %s, org %s, provider %s "
        "(nango_provider=%s, nango_integration=%s)",
        user_ids.user_id,
        user_ids.organisation_id,
        integration_type.value,
        provider_key,
        integration_key,
    )
    return session_data


@router.post(
    "/authorize",
    response_model=GenericResponse[OAuthInitiateResponse],
    summary="Create Nango Connect Session for OAuth",
    description="""
    Creates a Nango Connect Session token for OAuth authorization (New Flow).

    **New Flow (Post July 31, 2025):**
    1. Frontend calls this endpoint to get session token
    2. Frontend uses Nango Connect UI with the session token
    3. User authorizes on provider's site via Nango UI
    4. Nango sends webhook to backend with connection_id
    5. Backend stores connection and notifies frontend
    """,
)
async def initiate_oauth_flow(
    request: OAuthInitiateRequest,
    user_ids: UserIDs = Depends(get_current_user),
    user: User = Depends(user_auth),
) -> GenericResponse[OAuthInitiateResponse]:
    """
    Create a Nango Connect Session for OAuth authorization.

    New Flow (Post July 31, 2025):
    1. Backend creates a Connect Session and returns session token
    2. Frontend uses session token with Nango Connect UI
    3. Nango sends webhook to backend with connection_id after successful auth

    OAuth scopes are automatically determined from integration metadata configuration.
    """
    try:
        metadata = _get_validated_metadata(request.integration_type)
        _ensure_integration_available(metadata)

        nango_provider_key = to_nango_provider_key(request.integration_type)
        nango_integration_key = to_nango_connect_integration_key(request.integration_type)

        nango = NangoClient()
        session_data = await _create_nango_connect_session(
            nango=nango,
            user_ids=user_ids,
            user=user,
            integration_key=nango_integration_key,
            provider_key=nango_provider_key,
            integration_type=request.integration_type,
        )

        token, expires_at = _extract_session_details(session_data)
        response_data = OAuthInitiateResponse(
            session_token=token,
            integration_type=request.integration_type,
            expires_at=expires_at,
        )

        return Response.success_response(
            message="Connect session created successfully.",
            data=response_data,
        )
    except IntegrationUnavailableError as unavailable:
        logger.warning(
            "OAuth initiation attempted for unavailable integration %s (status=%s)",
            request.integration_type.value,
            unavailable.status,
        )
        return Response.bad_request_error_response(
            message=IntegrationErrors.INTEGRATION_NOT_AVAILABLE
        )
    except NangoConnectionError as e:
        error_detail = _extract_nango_error_message(e.payload)
        should_log_stack = _should_include_stacktrace(e)
        logger.error(
            "Failed to initiate OAuth for user %s, org %s, provider %s "
            "(status=%s, error=%s, payload=%s)",
            user_ids.user_id,
            user_ids.organisation_id,
            request.integration_type.value,
            e.status_code,
            error_detail or str(e),
            e.payload,
            exc_info=should_log_stack,
        )
        if e.status_code == 400:
            message = error_detail or IntegrationErrors.INTEGRATION_PROVIDER_NOT_CONFIGURED
            return Response.bad_request_error_response(message=message)
        return Response.internal_server_error_response(
            message=IntegrationErrors.OAUTH_INITIATION_FAILED
        )
    except Exception as e:
        logger.error(
            "Failed to initiate OAuth for user %s, org %s, provider %s: %s",
            user_ids.user_id,
            user_ids.organisation_id,
            request.integration_type.value,
            str(e),
            exc_info=True,
        )
        return Response.internal_server_error_response(
            message=IntegrationErrors.OAUTH_INITIATION_FAILED
        )


def _extract_integration_email(
    integration_type: IntegrationType, connection_metadata: dict[str, Any]
) -> str | None:
    """
    Extract the integration account email from provider-specific metadata.

    Args:
        integration_type: Type of integration
        connection_metadata: Metadata from Nango connection

    Returns:
        Email address of the connected account, or None if not available
    """
    logger.info(
        f"Extracting email for {integration_type.value} from metadata keys: {list(connection_metadata.keys())}"
    )

    # Google services (Drive, Calendar, etc.)
    if integration_type in [
        IntegrationType.GOOGLE_DRIVE,
        IntegrationType.GOOGLE_CALENDAR,
    ]:
        email = connection_metadata.get("email") or connection_metadata.get("user_email")
        logger.info(f"Google service email extraction: {email}")
        return email

    # HubSpot
    elif integration_type == IntegrationType.HUBSPOT:
        return connection_metadata.get("user") or connection_metadata.get("hub_email")

    # Slack
    elif integration_type == IntegrationType.SLACK:
        user_info = connection_metadata.get("user", {})
        return user_info.get("email") if isinstance(user_info, dict) else None

    # Salesforce
    elif integration_type == IntegrationType.SALESFORCE:
        return connection_metadata.get("email") or connection_metadata.get("username")

    # Microsoft services (SharePoint, OneDrive, Outlook)
    elif integration_type in [
        IntegrationType.SHAREPOINT,
        IntegrationType.ONEDRIVE,
        IntegrationType.OUTLOOK_CALENDAR,
    ]:
        return connection_metadata.get("email") or connection_metadata.get("userPrincipalName")

    # Calendly
    elif integration_type == IntegrationType.CALENDLY:
        # Calendly user object structure
        user_info = connection_metadata.get("user", {})
        return (
            user_info.get("email")
            if isinstance(user_info, dict)
            else connection_metadata.get("email")
        )

    else:
        # Generic fallback for other providers
        email = connection_metadata.get("email")
        logger.info(f"Generic fallback email extraction: {email}")
        return email


async def _get_integration_connection_details(
    nango: NangoClient,
    integration_type: IntegrationType,
    connection_id: str,
    provider_config_key: str,
) -> tuple[dict[str, Any], str | None]:
    """
    Fetch connection details from Nango and extract integration email.

    Args:
        nango: Nango client instance
        integration_type: The integration type enum
        connection_id: The Nango connection ID
        provider_config_key: The actual provider config key from Nango webhook

    Returns:
        Tuple of (connection_metadata, integration_email)
    """
    try:
        logger.info(
            f"Fetching Nango connection: provider={provider_config_key}, connection_id={connection_id}"
        )

        nango_connection = await nango.get_connection(
            provider=provider_config_key, connection_id=connection_id
        )

        logger.info(f"Full Nango connection response: {nango_connection}")

        # Try to extract email from multiple possible locations in Nango response
        connection_metadata = (
            nango_connection.get("connection_metadata") or nango_connection.get("metadata") or {}
        )
        end_user = nango_connection.get("end_user", {})

        logger.info(f"Connection metadata: {connection_metadata}")
        logger.info(f"End user data: {end_user}")

        # First try extracting from connection_metadata (provider-specific)
        integration_email = _extract_integration_email(integration_type, connection_metadata)

        # If not found in metadata, check end_user object (Nango's standard location)
        if not integration_email and end_user:
            integration_email = end_user.get("email")
            logger.info(f"Email extracted from end_user: {integration_email}")

        logger.info(
            f"Final extracted integration_email='{integration_email}' from {integration_type.value} connection"
        )

        return connection_metadata, integration_email

    except Exception as e:
        logger.error(
            f"Failed to get connection details from Nango for {integration_type.value}: {str(e)}. "
            f"Continuing without integration_email.",
            exc_info=True,
        )
        return {}, None


@router.delete(
    "/disconnect/{integration_id}",
    response_model=GenericResponse[None],
    summary="Disconnect and delete an integration",
    description="""
    Revokes OAuth tokens and deletes the integration.

    **Warning:** This action is irreversible. All sync history will be deleted.
    """,
)
async def disconnect_integration(
    integration_id: UUID,
    session: AsyncSession = Depends(get_org_async_session),
    user_ids: UserIDs = Depends(get_current_user),
) -> GenericResponse[None]:
    """
    Disconnect integration and revoke tokens.

    This will:
    1. Delete the connection from Nango (revoke tokens)
    2. Delete the integration record from database
    3. Cascade delete all sync history
    """
    try:
        nango = NangoClient()
        integration_service = IntegrationService(
            db_session=session,
            organisation_id=user_ids.organisation_id,
            nango_client=nango,
        )

        # First, get the integration to check access
        integration = await integration_service.get_integration(integration_id=integration_id)

        if not integration:
            logger.warning(
                f"Integration {integration_id} not found for org {user_ids.organisation_id}"
            )
            return Response.not_found_response(message=IntegrationErrors.INTEGRATION_NOT_FOUND)

        # Check access: user can only disconnect org-wide or their own user-scoped integrations
        if (
            integration.integration_scope == IntegrationScope.USER
            and integration.created_by_user_id != user_ids.user_id
        ):
            logger.warning(
                f"User {user_ids.user_id} attempted to disconnect user-scoped integration "
                f"{integration_id} created by {integration.created_by_user_id}"
            )
            return Response.forbidden_error_response(
                message="You don't have permission to disconnect this integration"
            )

        # Delete integration (includes Nango cleanup)
        await integration_service.delete_integration(
            integration_id=integration_id,
        )

        logger.info(f"Disconnected integration {integration_id} for org {user_ids.organisation_id}")

        return Response.success_response(message="Integration disconnected successfully", data=None)

    except ValueError as e:
        logger.warning(
            f"Integration {integration_id} not found for org {user_ids.organisation_id}: {str(e)}"
        )
        return Response.not_found_response(message=IntegrationErrors.INTEGRATION_NOT_FOUND)
    except Exception as e:
        logger.error(
            f"Failed to disconnect integration {integration_id} "
            f"for org {user_ids.organisation_id}: {str(e)}",
            exc_info=True,
        )
        return Response.internal_server_error_response(
            message=IntegrationErrors.INTEGRATION_DISCONNECTION_FAILED
        )


@router.post(
    "/webhooks/nango",
    response_model=None,
    summary="Nango Webhook Handler",
    description="""
    Webhook endpoint for receiving notifications from Nango after OAuth authorization.

    When a user completes OAuth authorization in Nango's UI:
    1. Nango sends POST request to this webhook with connection details and X-Nango-Signature header
    2. We verify the webhook signature for security
    3. We extract the connection_id and user metadata
    4. We create the integration record in our database
    5. We return 200 OK to acknowledge receipt

    **Important**: This endpoint must be publicly accessible and does NOT require authentication.
    Instead, it uses Nango's webhook signature verification (X-Nango-Signature header).

    For local development:
    - Use ngrok: `ngrok http 8000`
    - Configure webhook URL in Nango dashboard with ngrok URL
    - Example: https://abc123.ngrok.io/api/v1/users/integrations/oauth/webhooks/nango
    - Get webhook secret from Nango Dashboard > Environment Settings > Webhook Settings
    """,
)
async def handle_nango_webhook(
    request: Request,
    x_nango_signature: str = Header(None, alias="X-Nango-Signature"),
) -> Response:
    """
    Handle webhook notifications from Nango after successful OAuth authorization.

    This endpoint is called by Nango's servers (not by frontend) after a user
    completes the OAuth flow. It creates the integration record in our database.

    **Security**:
    - Does NOT use our JWT authentication (Nango doesn't have our tokens)
    - Instead verifies X-Nango-Signature header using HMAC-SHA256
    - Webhook secret must be configured in environment variables

    **Data Flow**:
    - organization_id is extracted from the webhook payload (endUser.tags.organizationId)
    - Database session is created using that organization_id
    """
    try:
        # Parse the request body
        payload = await request.json()

        # Log full webhook payload for debugging
        logger.info("NANGO WEBHOOK RECEIVED")
        logger.info(
            f"Connection ID: {payload.get('connectionId')}, Webhook Type: {payload.get('type')}"
        )
        logger.info(
            f"Connection ID: {payload.get('connectionId')}, Provider Config Key: {payload.get('providerConfigKey')}"
        )
        logger.info(f"Full Payload: {payload}")

        # Verify webhook signature for security
        if not NangoClient.verify_webhook_signature(x_nango_signature, payload):
            logger.error(f"Invalid webhook signature. Header: {x_nango_signature}")
            return Response.unauthorized_error_response(message="Invalid webhook signature")

        # Validate webhook payload
        if not payload.get("success"):
            logger.warning(f"Received failed authorization webhook: {payload}")
            return Response.success_response(
                message="Webhook received (failed authorization)", data=None
            )
        connection_id = payload.get("connectionId")
        provider_config_key = payload.get("providerConfigKey")
        webhook_type = payload.get("type")
        if webhook_type == "sync":
            logger.info(
                "Sync webhook received for connection %s (provider=%s). "
                "No user identifiers expected; acknowledging without DB changes.",
                connection_id,
                provider_config_key,
            )
            return Response.success_response(message="Sync webhook acknowledged", data=None)
        # Extract required fields for auth webhooks
        end_user = payload.get("endUser", {})

        if not connection_id or not provider_config_key:
            logger.error(f"Missing required fields in webhook: {payload}")
            return Response.bad_request_error_response(
                message="Missing required fields: connectionId or providerConfigKey"
            )

        # Extract user identifiers from endUser
        # Note: Nango sends "endUserId" not "id" in webhook payload
        user_id = end_user.get("endUserId")
        tags = end_user.get("tags", {})
        organization_id = tags.get("organizationId")

        if not user_id or not organization_id:
            logger.error(f"Missing user identifiers in webhook: {payload}")
            return Response.bad_request_error_response(
                message="Missing user identifiers in endUser"
            )

        # Convert provider_config_key to our IntegrationType
        try:
            # Use mapping helper to convert Nango provider key back to our enum
            integration_type = from_nango_provider_key(provider_config_key)
        except ValueError:
            logger.error(f"Unknown provider config key: {provider_config_key}")
            return Response.bad_request_error_response(
                message=f"Unknown integration type: {provider_config_key}"
            )

        # Step 1: Query user from master database to get role
        # (Users table is in master schema, not org schema)
        logger.info(f"Querying user {user_id} from master database...")
        async for master_session in acquire_master_async_session():
            from sqlalchemy import select

            statement = select(User).where(User.user_id == UUID(user_id))
            result = await master_session.execute(statement)
            user = result.scalar_one_or_none()

            if not user:
                logger.error(f"User {user_id} not found in master database")
                return Response.bad_request_error_response(message="User not found")

            # Log user details for debugging
            logger.info(
                f"User found: {user.email}, role={user.user_role}, org={user.organisation_id}"
            )

            # Determine access control fields
            # Logic: Admin creates org-wide, Member creates user-level
            created_by_admin = user.user_role == UserAppRole.ADMIN
            integration_scope = (
                IntegrationScope.ORGANIZATION if created_by_admin else IntegrationScope.USER
            )

            logger.info(
                f"Access control: created_by_admin={created_by_admin}, scope={integration_scope}"
            )

        # Step 2: Use organization session for integration operations
        # (Integrations table is in org schema)
        logger.info(f"Acquiring org session for organization {organization_id}...")
        async for org_session in acquire_org_async_session(UUID(organization_id)):
            # Initialize services
            nango = NangoClient()
            integration_service = IntegrationService(
                db_session=org_session,
                organisation_id=UUID(organization_id),
                nango_client=nango,
            )

            # Get connection details from Nango to extract integration email
            logger.info(f"Fetching connection details from Nango for connection {connection_id}...")
            connection_metadata, integration_email = await _get_integration_connection_details(
                nango, integration_type, connection_id, provider_config_key
            )

            logger.info(
                f"Connection metadata keys: {list(connection_metadata.keys()) if connection_metadata else 'None'}"
            )
            logger.info(f"Extracted integration_email: {integration_email}")

            # Create integration record with access control fields
            integration = await integration_service.create_integration(
                integration_type=integration_type,
                name=f"{integration_type.value.replace('_', ' ').title()} Integration",
                connection_id=connection_id,
                created_by_user_id=UUID(user_id),
                created_by_admin=created_by_admin,
                integration_scope=integration_scope,
                integration_email=integration_email,
                connection_metadata=connection_metadata,
            )

            logger.info(
                f"Created integration {integration.integration_id} for user {user_id} "
                f"(role={user.user_role.value}, admin={created_by_admin}, scope={integration_scope.value}, "
                f"email={integration_email}) in org {organization_id} via webhook"
            )

            return Response.success_response(
                message="Webhook processed successfully",
                data={
                    "integration_id": str(integration.integration_id),
                    "integration_type": integration_type.value,
                    "connection_id": connection_id,
                    "integration_scope": integration_scope.value,
                    "created_by_admin": created_by_admin,
                    "integration_email": integration_email,
                },
            )

    except Exception as e:
        logger.info("=" * 80)
        logger.info("WEBHOOK PROCESSING FAILED")
        logger.info("=" * 80)
        logger.info(f"Exception Type: {type(e).__name__}")
        logger.info(f"Exception Message: {str(e)}")
        logger.info("Full Traceback:")
        logger.info(exc_info=True)
        logger.info("=" * 80)
        # Return 200 OK even on error to prevent Nango retries
        # Log the error for investigation
        return Response.success_response(
            message="Webhook received but processing failed", data={"error": str(e)}
        )
