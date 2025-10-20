"""
OAuth flow routes for integration management.

Handles:
- Initiating OAuth flows via Nango Connect UI
- Webhook processing from Nango after authorization
- Connection setup and validation
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.integrations import (
    OAuthInitiateRequest,
    OAuthInitiateResponse,
)
from src.api.utils.http_response import Response
from src.domain.models.master_tables import User
from src.domain.models.organisation_tables.sync import (
    IntegrationScope,
)
from src.infra.auth.user_auth import user_auth
from src.infra.config.constant import IntegrationErrors
from src.infra.utils.logger import get_logger
from src.service.integrations import IntegrationService, NangoClient, webhook_service
from src.service.integrations.nango_client import NangoConnectionError
from src.service.integrations.oauth_service import (
    IntegrationUnavailableError,
    OAuthIntegrationService,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/oauth", tags=["integrations-oauth"])

oauth_service = OAuthIntegrationService()


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
        response_data = await oauth_service.create_connect_session(
            user_ids=user_ids,
            user=user,
            integration_type=request.integration_type,
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
        error_detail = OAuthIntegrationService.extract_nango_error_message(e.payload)
        should_log_stack = (e.status_code is None) or (
            isinstance(e.status_code, int) and e.status_code >= 500
        )
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
    payload = await request.json()
    return await webhook_service.handle_webhook(payload, x_nango_signature)
