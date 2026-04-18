from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi import Response as FastAPIResponse
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.integrations import OAuthInitiateRequest
from src.api.utils.http_response import Response
from src.infra.config.configs import app_config
from src.infra.config.constant import IntegrationErrors
from src.infra.utils.logger import get_logger
from src.service.integrations import IntegrationService
from src.service.integrations.copilot_callback_service import (
    CopilotCallbackService,
    copilot_callback_service,
)
from src.service.integrations.copilot_oauth_service import (
    CopilotOAuthIntegrationUnavailableError,
    CopilotOAuthService,
    copilot_oauth_service,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/copilot-oauth", tags=["copilot-integrations-oauth"])

# Frontend route that receives the OAuth result and signals the parent window to close the popup
_POPUP_COMPLETE_PATH = "/integrations/oauth/popup-complete"


async def get_copilot_oauth_service() -> CopilotOAuthService:
    return copilot_oauth_service


async def get_copilot_callback_service() -> CopilotCallbackService:
    return copilot_callback_service


def _build_popup_completion_redirect(
    payload: dict[str, str],
    *,
    status_code: int = 303,
) -> RedirectResponse:
    """Redirect the OAuth popup to the frontend completion page with result params.

    Uses 303 (See Other) to force the browser to GET the frontend URL regardless
    of the original callback's HTTP method.
    """
    app_url = (app_config.APP_URL or "").rstrip("/")
    if not app_url:
        raise RuntimeError("APP_URL must be configured for popup completion redirects")

    query = urlencode(payload)
    redirect_url = f"{app_url}{_POPUP_COMPLETE_PATH}?{query}"
    return RedirectResponse(url=redirect_url, status_code=status_code)


@router.post(
    "/authorize",
    response_model=GenericResponse[dict],
    summary="Create authorization URL for custom OAuth",
    description="Generates an OAuth authorization URL for the requested integration provider.",
)
async def initiate_custom_oauth_flow(
    request: OAuthInitiateRequest,
    response: FastAPIResponse,
    user_ids: UserIDs = Depends(get_current_user),
    oauth_service: CopilotOAuthService = Depends(get_copilot_oauth_service),
    _rl: None = Depends(rate_limit_user_write),
) -> GenericResponse[dict]:
    try:
        auth_url = await oauth_service.initiate_oauth_flow(
            user_ids=user_ids,
            integration_type=request.integration_type,
        )
        return Response.success_response(
            message="Authorization URL generated successfully.",
            data={"auth_url": auth_url},
        )
    except CopilotOAuthIntegrationUnavailableError as unavailable:
        logger.warning(
            "Custom OAuth initiation attempted for unavailable integration %s (status=%s)",
            request.integration_type.value,
            unavailable.status,
        )
        return Response.bad_request_error_response(
            message=IntegrationErrors.INTEGRATION_NOT_AVAILABLE, response=response
        )
    except ValueError as e:
        logger.warning(
            "Failed to initiate custom OAuth for user %s org %s: %s",
            user_ids.user_id,
            user_ids.organisation_id,
            str(e),
        )
        return Response.bad_request_error_response(message=str(e), response=response)
    except Exception as e:
        logger.error("Failed to initiate custom OAuth: %s", e, exc_info=True)
        return Response.internal_server_error_response(
            message=IntegrationErrors.OAUTH_INITIATION_FAILED,
            response=response,
        )


@router.get(
    "/callback",
    summary="OAuth Callback Handler",
    description="Handles the redirect from the OAuth provider with the authorization code and state.",
    response_class=RedirectResponse,
)
async def handle_oauth_callback(
    code: str,
    state: str,
    callback_service: CopilotCallbackService = Depends(get_copilot_callback_service),
) -> RedirectResponse:
    """Handle the provider's OAuth redirect — unauthenticated, trust comes from the signed JWT state."""
    try:
        result = await callback_service.handle_oauth_callback(code, state)
        return _build_popup_completion_redirect(
            {
                "success": "true",
                "message": "OAuth callback processed successfully",
                "integration_id": str(result["integration_id"]),
                "integration_type": str(result["integration_type"]),
            },
            status_code=303,
        )
    except ValueError as e:
        logger.warning("Rejected custom OAuth callback: %s", str(e))
        return _build_popup_completion_redirect(
            {
                "success": "false",
                "message": str(e),
            },
            status_code=303,
        )
    except Exception as e:
        logger.error("Failed to process custom OAuth callback: %s", e, exc_info=True)
        return _build_popup_completion_redirect(
            {
                "success": "false",
                "message": IntegrationErrors.OAUTH_CALLBACK_FAILED,
            },
            status_code=303,
        )


async def get_integration_service(
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency to create IntegrationService instance."""
    yield IntegrationService(db_session=session, user_ids=user_ids)


@router.delete(
    "/disconnect/{integration_id}",
    response_model=GenericResponse[None],
    summary="Disconnect and deactivate an integration",
    description="Revokes OAuth tokens and marks the integration as inactive.",
)
async def disconnect_integration(
    integration_id: UUID,
    response: FastAPIResponse,
    service: IntegrationService = Depends(get_integration_service),
    _rl: None = Depends(rate_limit_user_write),
) -> GenericResponse[None]:
    """Revoke OAuth tokens at the provider and mark the integration as inactive."""
    try:
        integration = await service.get_integration(integration_id=integration_id)

        if not integration:
            return Response.not_found_error_response(
                message=IntegrationErrors.INTEGRATION_NOT_FOUND, response=response
            )

        # Only the user who created the integration can disconnect it
        # TODO(nikhil, 2026-Q2): Allow admins to disconnect any user's integration
        # by checking user role — admins bypass this check, members cannot
        if integration.created_by_user_id != service.user_ids.user_id:
            return Response.forbidden_error_response(
                message="You can only disconnect integrations you created.",
                response=response,
            )

        if integration.is_active is False:
            return Response.bad_request_error_response(
                message=IntegrationErrors.INTEGRATION_ALREADY_INACTIVE, response=response
            )

        if not integration.connection_id:
            return Response.bad_request_error_response(
                message="Integration has no connection ID", response=response
            )

        await service.revoke_custom_oauth_token(integration)

        integration.is_active = False
        integration.requires_reauth = False
        integration.last_sync_error = "Integration disconnected by user"
        service.db.add(integration)
        await service.db.commit()

        logger.info(
            "Disconnected integration %s for org %s",
            integration_id,
            service.user_ids.organisation_id,
        )
        return Response.success_response(
            message="Integration disconnected and marked as inactive successfully", data=None
        )

    except ValueError as e:
        logger.warning("Integration %s not found: %s", integration_id, str(e))
        return Response.not_found_error_response(
            message=IntegrationErrors.INTEGRATION_NOT_FOUND, response=response
        )
    except Exception as e:
        logger.error(
            "Failed to disconnect integration %s: %s",
            integration_id,
            str(e),
            exc_info=True,
        )
        return Response.internal_server_error_response(
            message=IntegrationErrors.INTEGRATION_DISCONNECTION_FAILED, response=response
        )
