"""
OAuth flow routes for integration management.

Handles:
- Initiating OAuth flows via Nango Connect UI
- Webhook processing from Nango after authorization
- Connection setup and validation
"""

# mypy: disable-error-code="no-any-return,return-value"

from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from fastapi import Response as FastAPIResponse
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.integrations import (
    OAuthInitiateRequest,
    OAuthInitiateResponse,
)
from src.api.utils.http_response import Response
from src.domain.models.master_tables import User
from src.infra.auth.user_auth import user_auth
from src.infra.config.constant import IntegrationErrors
from src.infra.utils.logger import get_logger
from src.service.integrations import IntegrationService, NangoClient
from src.service.integrations.nango_client import NangoConnectionError
from src.service.integrations.nango_webhook_service import NangoWebhookService
from src.service.integrations.oauth_service import (
    IntegrationUnavailableError,
    OAuthIntegrationService,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/oauth", tags=["integrations-oauth"])


async def get_integration_service(
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency to create IntegrationService instance with proper cleanup."""
    nango = NangoClient()
    service = IntegrationService(db_session=session, user_ids=user_ids, nango_client=nango)
    try:
        yield service
    finally:
        await nango.close()
        if hasattr(service, "close") and callable(service.close):
            await service.close()


async def get_oauth_service():
    """Dependency to create OAuthIntegrationService instance with proper cleanup."""
    service = OAuthIntegrationService()
    try:
        yield service
    finally:
        # OAuthIntegrationService creates NangoClient internally
        # Future: refactor to accept NangoClient in __init__ for better resource management
        pass


async def get_webhook_service():
    """Dependency to create NangoWebhookService instance with proper cleanup."""
    service = NangoWebhookService()
    try:
        yield service
    finally:
        # NangoWebhookService creates NangoClient internally
        # Future: refactor to accept NangoClient in __init__ for better resource management
        pass


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
    response: FastAPIResponse,
    user: User = Depends(user_auth),
    oauth_service: OAuthIntegrationService = Depends(get_oauth_service),
    _rl: None = Depends(rate_limit_user_write),
) -> GenericResponse[OAuthInitiateResponse]:
    """
    Initiate OAuth flow by creating a Nango Connect Session.

    Returns a session token that the frontend uses with Nango Connect UI
    to complete the OAuth authorization flow.
    """
    try:
        # Extract user_ids from the authenticated user
        user_ids = UserIDs(user_id=user.user_id, organisation_id=user.organisation_id)

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
            message=IntegrationErrors.INTEGRATION_NOT_AVAILABLE, response=response
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
            return Response.bad_request_error_response(message=message, response=response)
        return Response.internal_server_error_response(
            message=IntegrationErrors.OAUTH_INITIATION_FAILED, response=response
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
            message=IntegrationErrors.OAUTH_INITIATION_FAILED, response=response
        )


@router.delete(
    "/disconnect/{integration_id}",
    response_model=GenericResponse[None],
    summary="Disconnect and deactivate an integration",
    description="""
    Revokes OAuth tokens and marks the integration as inactive.

    The integration will no longer appear in active integration lists or be used for syncing.
    Historical sync data is preserved for audit purposes.
    """,
)
async def disconnect_integration(
    integration_id: UUID,
    response: FastAPIResponse,
    service: IntegrationService = Depends(get_integration_service),
    _rl: None = Depends(rate_limit_user_write),
) -> GenericResponse[None]:
    """
    Disconnect and deactivate an integration.

    Revokes OAuth connection in Nango and marks the integration as inactive locally.
    Users can only disconnect organization-wide integrations or their own user-scoped ones.
    """
    try:
        # First, get the integration to check access
        integration = await service.get_integration(integration_id=integration_id)

        if not integration:
            logger.warning(
                f"Integration {integration_id} not found for org {service.user_ids.organisation_id}"
            )
            return Response.not_found_error_response(
                message=IntegrationErrors.INTEGRATION_NOT_FOUND, response=response
            )

        if integration.is_active is False:
            logger.warning(f"Integration {integration_id} is does not exist or is already inactive")
            return Response.bad_request_error_response(
                message=IntegrationErrors.INTEGRATION_ALREADY_INACTIVE, response=response
            )

        # Validate connection_id exists before deletion
        if not integration.connection_id:
            logger.error(
                f"Integration {integration_id} has no connection_id, cannot delete from Nango"  # noqa: S608  # nosec B608
            )
            return Response.bad_request_error_response(
                message="Integration has no connection ID", response=response
            )

        # Delete integration (includes Nango cleanup)
        await service.delete_integration_from_nango(
            connection_id=integration.connection_id,
            integration_type=integration.integration_type,
        )

        # Soft delete: mark as inactive
        integration.is_active = False
        integration.last_sync_error = "Integration disconnected by user"
        service.db.add(integration)
        await service.db.commit()

        # Log disconnection
        logger.info(
            f"Disconnected and deactivated integration {integration_id} for org {service.user_ids.organisation_id}"
        )
        return Response.success_response(
            message="Integration disconnected and marked as inactive successfully", data=None
        )

    except ValueError as e:
        logger.warning(
            f"Integration {integration_id} not found for org {service.user_ids.organisation_id}: {str(e)}"
        )
        return Response.not_found_error_response(
            message=IntegrationErrors.INTEGRATION_NOT_FOUND, response=response
        )
    except Exception as e:
        logger.error(
            f"Failed to disconnect integration {integration_id} "
            f"for org {service.user_ids.organisation_id}: {str(e)}",
            exc_info=True,
        )
        return Response.internal_server_error_response(
            message=IntegrationErrors.INTEGRATION_DISCONNECTION_FAILED, response=response
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
    webhook_service: NangoWebhookService = Depends(get_webhook_service),
) -> JSONResponse:
    try:
        # Extract JSON payload
        payload = await request.json()
        if not webhook_service._is_valid_signature(x_nango_signature, payload):
            logger.error("Unauthorized Nango webhook attempt detected.")
            return Response.unauthorized_error_response(message="Invalid webhook signature.")

        # Check success flag
        if not payload.get("success"):
            logger.error("Received unsuccessful Nango webhook: %s", payload)
            return Response.bad_request_error_response(message="Unsuccessful webhook payload.")

        # Determine webhook type and process accordingly
        # NOTE: Sync webhooks removed - now handled by Celery Beat scheduled tasks
        # See: docs/Technical Documentation/integration/sync_tasks/scheduled-sync-architecture.md
        webhook_type = payload.get("type")
        match webhook_type:
            case "auth":
                return await webhook_service._handle_authorization_webhook(payload)
            case "sync":
                # Sync webhooks are handled at application level via Celery Beat scheduled tasks
                # See: docs/Technical Documentation/integration/sync_tasks/scheduled-sync-architecture.md
                logger.info(
                    "Received sync webhook - handled at application level via scheduled tasks"
                )
                return JSONResponse(status_code=200, content={"status": "Handeled at app level"})
            case _:
                logger.error("Received unknown Nango webhook type: %s", webhook_type)
                return Response.bad_request_error_response(message="Unknown webhook type.")
    except Exception as e:
        logger.error("Failed to process Nango webhook: %s", str(e), exc_info=True)
        return Response.internal_server_error_response(message="Failed to process webhook.")
