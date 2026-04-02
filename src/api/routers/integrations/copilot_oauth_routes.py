from urllib.parse import urlencode

from fastapi import APIRouter, Depends
from fastapi import Response as FastAPIResponse
from fastapi.responses import RedirectResponse
from src.api.dependencies.organisation_dependencies import UserIDs
from src.api.dependencies.rate_limit import rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.integrations import OAuthInitiateRequest
from src.api.utils.http_response import Response
from src.domain.models.master_tables import User
from src.infra.auth.user_auth import user_auth
from src.infra.config.configs import app_config
from src.infra.config.constant import IntegrationErrors
from src.infra.utils.logger import get_logger
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
    user: User = Depends(user_auth),
    oauth_service: CopilotOAuthService = Depends(get_copilot_oauth_service),
    _rl: None = Depends(rate_limit_user_write),
) -> GenericResponse[dict]:
    try:
        user_ids = UserIDs(user_id=user.user_id, organisation_id=user.organisation_id)
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
            user.user_id,
            user.organisation_id,
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
