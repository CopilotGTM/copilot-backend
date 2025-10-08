"""
OAuth flow routes for integration management.

Handles:
- Initiating OAuth flows
- OAuth callback processing
- Connection setup and validation
"""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies.organisation_dependencies import (
    get_current_user,
    get_org_async_session,
    UserIDs,
)
from src.api.schemas.base_models import GenericResponse
from src.api.utils.http_response import Response
from src.api.schemas.integrations import (
    OAuthInitiateRequest,
    OAuthInitiateResponse,
    IntegrationResponse,
    IntegrationError,
)
from src.service.integrations import NangoClient, IntegrationService
from src.infra.config.configs import nango_integration_config
from src.infra.utils.logger import get_logger
from src.infra.config.constant import IntegrationErrors
from src.domain.models.organisation_tables.knowledge_models import IntegrationType

logger = get_logger(__name__)
router = APIRouter(prefix="/oauth", tags=["integrations-oauth"])


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
        nango = NangoClient()

        # Get integration metadata to retrieve default OAuth scopes
        from src.infra.config.integrations_metadata import get_integration_metadata

        metadata = get_integration_metadata(request.integration_type)
        if not metadata:
            raise ValueError(f"Unknown integration type: {request.integration_type}")

        # Create Connect Session with Nango
        session_data = await nango.create_connect_session(
            end_user_id=str(user_ids.user_id),
            organization_id=str(user_ids.organisation_id),
            integration_id=request.integration_type.value,
            end_user_email=None,  # Can be added if available
            end_user_display_name=None,  # Can be added if available
        )

        logger.info(
            f"Created Connect Session for user {user_ids.user_id}, "
            f"org {user_ids.organisation_id}, provider {request.integration_type.value}"
        )

        # Return session token for frontend to use with Nango Connect UI
        response_data = OAuthInitiateResponse(
            session_token=session_data.get("token"),
            integration_type=request.integration_type,
            expires_at=session_data.get("expires_at"),
        )

        return Response.success_response(
            message="Connect session created successfully. Use session_token with Nango Connect UI.",
            data=response_data,
        )

    except Exception as e:
        logger.error(
            f"Failed to initiate OAuth for user {user_ids.user_id}, "
            f"org {user_ids.organisation_id}: {str(e)}",
            exc_info=True,
        )
        return Response.internal_server_error_response(
            message=IntegrationErrors.OAUTH_INITIATION_FAILED
        )


@router.get(
    "/callback",
    response_class=RedirectResponse,
    summary="OAuth callback endpoint",
    description="""
    Receives OAuth callback from provider after user authorization.
    
    **Flow:**
    1. Provider redirects here with authorization code
    2. Backend exchanges code for tokens via Nango
    3. Backend creates integration record in database
    4. Redirects user back to frontend success page
    
    **Note:** This endpoint is called by the OAuth provider, not the frontend.
    """,
)
async def oauth_callback(
    code: str = Query(..., description="Authorization code from OAuth provider"),
    state: Optional[str] = Query(None, description="State parameter for validation"),
    connection_id: Optional[str] = Query(None, description="Connection identifier"),
    session: AsyncSession = Depends(get_org_async_session),
    user_ids: UserIDs = Depends(get_current_user),
) -> RedirectResponse:
    """
    Handle OAuth callback and create integration.

    This endpoint is hit by the OAuth provider after user authorization.
    It exchanges the code for tokens and creates the integration record.
    """
    try:
        # Extract integration type from connection_id
        # Format: org_{org_id}_{integration_type}_{timestamp}
        if not connection_id:
            raise ValueError("Missing connection_id in callback")

        parts = connection_id.split("_")
        if len(parts) < 3:
            raise ValueError("Invalid connection_id format")

        integration_type = parts[2]  # Extract integration type

        # Initialize services
        nango = NangoClient()
        integration_service = IntegrationService(
            db_session=session,
            organisation_id=user_ids.organisation_id,
            nango_client=nango,
        )

        # Nango automatically exchanges the code for tokens via webhooks,
        # so we just need to verify the connection exists
        connection = await nango.get_connection(
            provider=integration_type,
            connection_id=connection_id,
        )

        # Get metadata from connection
        metadata = connection.get("metadata", {})
        credentials = connection.get("credentials", {})

        # Create integration record
        from src.domain.models.organisation_tables.knowledge_models import (
            IntegrationType,
        )

        integration = await integration_service.create_integration(
            integration_type=IntegrationType(integration_type),
            name=f"{integration_type.title()} Integration",  # Default name, user can change later
            connection_id=connection_id,
            connection_metadata=metadata,
            scopes=credentials.get("scopes", []),
        )

        logger.info(
            f"Successfully created integration {integration.integration_id} "
            f"for org {user_ids.organisation_id}"
        )

        # Redirect to frontend success page
        frontend_url = (
            nango_integration_config.OAUTH_CALLBACK_BASE_URL or "http://localhost:3000"
        )
        success_url = f"{frontend_url}/settings/integrations?status=success&integration_id={integration.integration_id}"

        return RedirectResponse(url=success_url)

    except Exception as e:
        logger.error(f"OAuth callback failed: {str(e)}", exc_info=True)

        # Redirect to frontend error page
        frontend_url = (
            nango_integration_config.OAUTH_CALLBACK_BASE_URL or "http://localhost:3000"
        )
        error_url = (
            f"{frontend_url}/settings/integrations?status=error&message={str(e)}"
        )

        return RedirectResponse(url=error_url)


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

        # Delete integration (includes Nango cleanup)
        await integration_service.delete_integration(
            integration_id=integration_id,
        )

        logger.info(
            f"Disconnected integration {integration_id} "
            f"for org {user_ids.organisation_id}"
        )

        return Response.success_response(
            message="Integration disconnected successfully", data=None
        )

    except ValueError as e:
        logger.warning(
            f"Integration {integration_id} not found for org {user_ids.organisation_id}: {str(e)}"
        )
        return Response.not_found_response(
            message=IntegrationErrors.INTEGRATION_NOT_FOUND
        )
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
    1. Nango sends POST request to this webhook with connection details
    2. We extract the connection_id and user metadata
    3. We create the integration record in our database
    4. We return 200 OK to acknowledge receipt
    
    **Important**: This endpoint must be publicly accessible. For local development:
    - Use ngrok: `ngrok http 8000`
    - Configure webhook URL in Nango dashboard with ngrok URL
    - Example: https://abc123.ngrok.io/api/v1/users/integrations/oauth/webhooks/nango
    """,
)
async def handle_nango_webhook(
    request: dict,
    session: AsyncSession = Depends(get_org_async_session),
) -> Response:
    """
    Handle webhook notifications from Nango after successful OAuth authorization.

    This endpoint is called by Nango's servers (not by frontend) after a user
    completes the OAuth flow. It creates the integration record in our database.
    """
    try:
        logger.info(f"Received Nango webhook: {request}")

        # Validate webhook payload
        if not request.get("success"):
            logger.warning(f"Received failed authorization webhook: {request}")
            return Response.success_response(
                message="Webhook received (failed authorization)", data=None
            )

        # Extract required fields
        connection_id = request.get("connectionId")
        provider_config_key = request.get("providerConfigKey")
        end_user = request.get("endUser", {})

        if not connection_id or not provider_config_key:
            logger.error(f"Missing required fields in webhook: {request}")
            return Response.bad_request_response(
                message="Missing required fields: connectionId or providerConfigKey"
            )

        # Extract user identifiers from endUser
        user_id = end_user.get("id")
        tags = end_user.get("tags", {})
        organization_id = tags.get("organizationId")

        if not user_id or not organization_id:
            logger.error(f"Missing user identifiers in webhook: {request}")
            return Response.bad_request_response(
                message="Missing user identifiers in endUser"
            )

        # Convert provider_config_key to our IntegrationType
        try:
            # Map Nango provider keys to our IntegrationType enum
            # (e.g., "hubspot" -> IntegrationType.HUBSPOT)
            integration_type = IntegrationType(provider_config_key.upper())
        except ValueError:
            logger.error(f"Unknown provider config key: {provider_config_key}")
            return Response.bad_request_response(
                message=f"Unknown integration type: {provider_config_key}"
            )

        # Initialize services
        nango = NangoClient()
        integration_service = IntegrationService(
            db_session=session,
            organisation_id=UUID(organization_id),
            nango_client=nango,
        )

        # Create integration record
        integration = await integration_service.create_integration(
            integration_type=integration_type,
            user_id=UUID(user_id),
            nango_connection_id=connection_id,
        )

        logger.info(
            f"Created integration {integration.id} for user {user_id} "
            f"in org {organization_id} via webhook"
        )

        return Response.success_response(
            message="Webhook processed successfully",
            data={
                "integration_id": str(integration.id),
                "integration_type": integration_type.value,
                "connection_id": connection_id,
            },
        )

    except Exception as e:
        logger.error(f"Failed to process Nango webhook: {str(e)}", exc_info=True)
        # Return 200 OK even on error to prevent Nango retries
        # Log the error for investigation
        return Response.success_response(
            message="Webhook received but processing failed", data={"error": str(e)}
        )
