"""
OAuth flow routes for integration management.

Handles:
- Initiating OAuth flows via Nango Connect UI
- Webhook processing from Nango after authorization
- Connection setup and validation
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Request, Header
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies.organisation_dependencies import (
    get_current_user,
    get_org_async_session,
    acquire_org_async_session,
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
from src.infra.auth.user_auth import user_auth
from src.domain.models.master_tables import User
from src.infra.config.configs import nango_integration_config
from src.infra.utils.logger import get_logger
from src.infra.config.constant import IntegrationErrors
from src.domain.models.organisation_tables.knowledge_models import IntegrationType
from src.infra.config.integrations_metadata import get_integration_metadata

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
        nango = NangoClient()     

        metadata = get_integration_metadata(request.integration_type)
        if not metadata:
            raise ValueError(f"Unknown integration type: {request.integration_type}")

        # Create Connect Session with Nango
        session_data = await nango.create_connect_session(
            end_user_id=str(user_ids.user_id),
            organization_id=str(user_ids.organisation_id),
            integration_id=request.integration_type.value,
            end_user_email=user.email,
            end_user_display_name=f"{user.first_name} {user.last_name}",
        )
        
        # Log the full session data at debug level
        logger.info(f"Nango Connect session data: {session_data}")

        logger.info(
            f"Created Connect Session for user {user_ids.user_id}, "
            f"org {user_ids.organisation_id}, provider {request.integration_type.value}"
        )

        # Normalize token/expires fields from Nango response
        raw = session_data.get("data") if isinstance(session_data, dict) else None
        payload = raw if isinstance(raw, dict) else session_data

        token = payload.get("token") or (payload.get("session") or {}).get("token")
        expires_at = payload.get("expires_at") or (payload.get("session") or {}).get("expires_at")

        if not token:
            logger.error(f"Nango connect session created but token missing. Payload: {session_data}")
            raise ValueError("Failed to retrieve session token")

        # Return session token for frontend to use with Nango Connect UI
        response_data = OAuthInitiateResponse(
            session_token=token,
            integration_type=request.integration_type,
            expires_at=expires_at,
        )

        return Response.success_response(
            message="Connect session created successfully.",
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
        logger.info(f"Received Nango webhook: type={payload.get('type')}, operation={payload.get('operation')}, connectionId={payload.get('connectionId')}")
        
        # Verify webhook signature for security
        if not NangoClient.verify_webhook_signature(x_nango_signature, payload):
            logger.error(f"Invalid webhook signature. Header: {x_nango_signature}")
            return Response.unauthorized_error_response(
                message="Invalid webhook signature"
            )

        # Validate webhook payload
        if not payload.get("success"):
            logger.warning(f"Received failed authorization webhook: {payload}")
            return Response.success_response(
                message="Webhook received (failed authorization)", data=None
            )

        # Extract required fields
        connection_id = payload.get("connectionId")
        provider_config_key = payload.get("providerConfigKey")
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
            # Map Nango provider keys to our IntegrationType enum
            # (e.g., "hubspot" -> IntegrationType.HUBSPOT)
            # Note: IntegrationType values are lowercase (e.g., "slack" not "SLACK")
            integration_type = IntegrationType(provider_config_key.lower())
        except ValueError:
            logger.error(f"Unknown provider config key: {provider_config_key}")
            return Response.bad_request_error_response(
                message=f"Unknown integration type: {provider_config_key}"
            )

        # Acquire organization-scoped session without authentication
        # (organization_id comes from webhook payload)
        async for session in acquire_org_async_session(UUID(organization_id)):
            # Initialize services
            nango = NangoClient()
            integration_service = IntegrationService(
                db_session=session,
                organisation_id=UUID(organization_id),
                nango_client=nango,
            )

            # Create integration record
            # Note: IntegrationService.create_integration() expects:
            # - integration_type, name, connection_id (not user_id or nango_connection_id)
            # - user_id is tracked in Nango's connection metadata, not in our Integration model
            integration = await integration_service.create_integration(
                integration_type=integration_type,
                name=f"{integration_type.value.title()} Integration",
                connection_id=connection_id,
            )

            logger.info(
                f"Created integration {integration.integration_id} for user {user_id} "
                f"in org {organization_id} via webhook"
            )

            return Response.success_response(
                message="Webhook processed successfully",
                data={
                    "integration_id": str(integration.integration_id),
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
