"""OAuth integration routes, including authenticated initial-sync progress."""

from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi import Response as FastAPIResponse
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.integrations import OAuthInitiateRequest
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables.sync import IntegrationSyncState
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
from src.service.integrations.initial_sync_sse_service import InitialSyncSSEService
from sse_starlette.sse import EventSourceResponse

logger = get_logger(__name__)

router = APIRouter(prefix="/copilot-oauth", tags=["copilot-integrations-oauth"])

# Frontend route that receives the OAuth result and signals the parent window to close the popup
_POPUP_COMPLETE_PATH = "/integrations/oauth/popup-complete"
_MAX_PROVIDER_ERROR_MESSAGE_LEN = 300
_MICROSOFT_ADMIN_CONSENT_PURPOSE = "microsoft_admin_consent"
_MICROSOFT_ADMIN_CONSENT_DENIED_MESSAGE = (
    "Admin consent was not granted. Sign in with a Microsoft tenant admin account "
    "or send the approval link to your admin."
)
_MICROSOFT_ADMIN_CONSENT_ERROR_MARKERS = (
    "need admin approval",
    "admin approval",
    "admin consent",
    "admin_consent",
    "aadsts65001",
)
_MICROSOFT_USER_OWNED_TYPES = {"microsoft", "outlook_email", "outlook_calendar", "teams"}


async def get_copilot_oauth_service() -> CopilotOAuthService:
    return copilot_oauth_service


async def get_copilot_callback_service() -> CopilotCallbackService:
    return copilot_callback_service


async def _extract_attempt_id_from_state(
    state: str,
    oauth_service: CopilotOAuthService,
) -> str | None:
    try:
        state_data = await oauth_service.decode_oauth_state_allow_expired(state)
    except Exception:
        return None

    jti = state_data.get("jti")
    return str(jti) if jti else None


async def _extract_oauth_state_context(
    state: str,
    oauth_service: CopilotOAuthService,
) -> dict[str, str]:
    try:
        state_data = await oauth_service.decode_oauth_state_allow_expired(state)
    except Exception:
        return {}

    context: dict[str, str] = {}
    for key in ("jti", "type", "purpose"):
        value = state_data.get(key)
        if value:
            context[key] = str(value)
    return context


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


def _provider_error_message(error: str, error_description: str | None = None) -> str:
    """Keep provider-supplied OAuth errors useful without allowing unbounded popup URLs."""
    normalized_error = " ".join(error.split())
    normalized_description = " ".join(error_description.split()) if error_description else None
    message = (
        f"{normalized_error}: {normalized_description}"
        if normalized_description
        else normalized_error
    )
    return message[:_MAX_PROVIDER_ERROR_MESSAGE_LEN]


def _is_microsoft_admin_consent_required_error(
    error: str,
    error_description: str | None,
) -> bool:
    combined = f"{error} {error_description or ''}".lower()
    if any(marker in combined for marker in _MICROSOFT_ADMIN_CONSENT_ERROR_MARKERS):
        return True

    # Some tenant-policy blocks come back as bare access_denied without the
    # "Need admin approval" text, which is the only signal the frontend receives.
    return error.strip().lower() == "access_denied" and not error_description


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
        result = await oauth_service.initiate_oauth_flow(
            user_ids=user_ids,
            integration_type=request.integration_type,
            oauth_base_url=request.oauth_base_url,
        )
        return Response.success_response(
            message="Authorization URL generated successfully.",
            data={"auth_url": result.auth_url, "attempt_id": result.attempt_id},
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


@router.post(
    "/microsoft/admin-consent",
    response_model=GenericResponse[dict],
    summary="Create Microsoft admin consent URL",
    description="Generates a Microsoft Entra admin-consent URL for a Microsoft integration.",
)
async def initiate_microsoft_admin_consent_flow(
    request: OAuthInitiateRequest,
    response: FastAPIResponse,
    user_ids: UserIDs = Depends(get_current_user),
    oauth_service: CopilotOAuthService = Depends(get_copilot_oauth_service),
    _rl: None = Depends(rate_limit_user_write),
) -> GenericResponse[dict]:
    try:
        result = await oauth_service.initiate_microsoft_admin_consent_flow(
            user_ids=user_ids,
            integration_type=request.integration_type,
        )
        return Response.success_response(
            message="Microsoft admin consent URL generated successfully.",
            data={"auth_url": result.auth_url, "attempt_id": result.attempt_id},
        )
    except ValueError as e:
        logger.warning(
            "Failed to initiate Microsoft admin consent for user %s org %s: %s",
            user_ids.user_id,
            user_ids.organisation_id,
            str(e),
        )
        return Response.bad_request_error_response(message=str(e), response=response)
    except Exception as e:
        logger.error("Failed to initiate Microsoft admin consent: %s", e, exc_info=True)
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
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    admin_consent: str | None = None,
    tenant: str | None = None,
    scope: str | None = None,
    callback_service: CopilotCallbackService = Depends(get_copilot_callback_service),
    oauth_service: CopilotOAuthService = Depends(get_copilot_oauth_service),
) -> RedirectResponse:
    """Handle the provider's OAuth redirect — unauthenticated, trust comes from the signed JWT state."""
    if error:
        message = _provider_error_message(error, error_description)
        logger.warning("Provider rejected custom OAuth callback: %s", message)
        payload = {
            "success": "false",
            "message": message,
        }
        if state:
            state_context = await _extract_oauth_state_context(state, oauth_service)
            attempt_id = state_context.get("jti")
            integration_type = state_context.get("type")
            purpose = state_context.get("purpose")
            if attempt_id:
                payload["attempt_id"] = attempt_id
            if integration_type:
                payload["integration_type"] = integration_type
            if (
                error.strip().lower() == "access_denied"
                and purpose == _MICROSOFT_ADMIN_CONSENT_PURPOSE
                and integration_type in _MICROSOFT_USER_OWNED_TYPES
            ):
                payload["message"] = _MICROSOFT_ADMIN_CONSENT_DENIED_MESSAGE
                payload["action_required"] = "admin_consent_required"
            if (
                integration_type in _MICROSOFT_USER_OWNED_TYPES
                and _is_microsoft_admin_consent_required_error(error, error_description)
            ):
                payload["action_required"] = "admin_consent_required"
        return _build_popup_completion_redirect(payload, status_code=303)

    if admin_consent is not None:
        if not state:
            return _build_popup_completion_redirect(
                {
                    "success": "false",
                    "message": "Microsoft admin consent callback did not include state.",
                },
                status_code=303,
            )
        try:
            result = await oauth_service.handle_microsoft_admin_consent_callback(
                state=state,
                admin_consent=admin_consent,
                tenant=tenant,
                scope=scope,
            )
            return _build_popup_completion_redirect(
                {
                    "success": "true",
                    "message": "Microsoft admin consent completed successfully",
                    "integration_type": str(result["integration_type"]),
                    "attempt_id": str(result["attempt_id"]),
                },
                status_code=303,
            )
        except ValueError as e:
            logger.warning("Rejected Microsoft admin consent callback: %s", str(e))
            payload = {
                "success": "false",
                "message": str(e),
            }
            attempt_id = await _extract_attempt_id_from_state(state, oauth_service)
            if attempt_id:
                payload["attempt_id"] = attempt_id
            return _build_popup_completion_redirect(payload, status_code=303)
        except Exception as e:
            logger.error("Failed to process Microsoft admin consent callback: %s", e, exc_info=True)
            payload = {
                "success": "false",
                "message": IntegrationErrors.OAUTH_CALLBACK_FAILED,
            }
            attempt_id = await _extract_attempt_id_from_state(state, oauth_service)
            if attempt_id:
                payload["attempt_id"] = attempt_id
            return _build_popup_completion_redirect(payload, status_code=303)

    if not code:
        logger.warning("Rejected custom OAuth callback: missing authorization code")
        payload = {
            "success": "false",
            "message": "OAuth callback did not include an authorization code.",
        }
        if state:
            attempt_id = await _extract_attempt_id_from_state(state, oauth_service)
            if attempt_id:
                payload["attempt_id"] = attempt_id
        return _build_popup_completion_redirect(payload, status_code=303)

    if not state:
        logger.warning("Rejected custom OAuth callback: missing state")
        return _build_popup_completion_redirect(
            {
                "success": "false",
                "message": "OAuth callback did not include state.",
            },
            status_code=303,
        )

    try:
        result = await callback_service.handle_oauth_callback(code, state)
        return _build_popup_completion_redirect(
            {
                "success": "true",
                "message": "OAuth callback processed successfully",
                "integration_id": str(result["integration_id"]),
                "integration_type": str(result["integration_type"]),
                "attempt_id": str(result["connection_id"]),
            },
            status_code=303,
        )
    except ValueError as e:
        logger.warning("Rejected custom OAuth callback: %s", str(e))
        payload = {
            "success": "false",
            "message": str(e),
        }
        attempt_id = await _extract_attempt_id_from_state(state, oauth_service)
        if attempt_id:
            payload["attempt_id"] = attempt_id
        return _build_popup_completion_redirect(
            payload,
            status_code=303,
        )
    except Exception as e:
        logger.error("Failed to process custom OAuth callback: %s", e, exc_info=True)
        payload = {
            "success": "false",
            "message": IntegrationErrors.OAUTH_CALLBACK_FAILED,
        }
        attempt_id = await _extract_attempt_id_from_state(state, oauth_service)
        if attempt_id:
            payload["attempt_id"] = attempt_id
        return _build_popup_completion_redirect(
            payload,
            status_code=303,
        )


async def get_integration_service(
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency to create IntegrationService instance."""
    yield IntegrationService(db_session=session, user_ids=user_ids)


def _initial_sync_sse_service(user_ids: UserIDs, sync_id: UUID) -> InitialSyncSSEService:
    return InitialSyncSSEService(user_ids.organisation_id, sync_id, user_ids.user_id)


async def _authorize_initial_sync(user_ids: UserIDs, sync_id: UUID) -> InitialSyncSSEService:
    service = _initial_sync_sse_service(user_ids, sync_id)
    can_access = await service.ensure_user_access()
    if can_access is None:
        raise HTTPException(status_code=404, detail="Initial sync run not found")
    if can_access is False:
        raise HTTPException(status_code=403, detail="Not authorized to access this initial sync")
    return service


@router.get("/initial-sync/{sync_id}", summary="Get durable initial sync progress")
async def get_initial_sync_progress(
    sync_id: UUID,
    user_ids: UserIDs = Depends(get_current_user),
) -> dict:
    """Return the latest durable initial-sync snapshot owned by this user."""
    service = await _authorize_initial_sync(user_ids, sync_id)
    snapshot = await service.get_snapshot()
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Initial sync run not found")
    return snapshot


@router.get("/initial-sync/{sync_id}/stream", summary="Stream initial sync progress")
async def stream_initial_sync_progress(
    sync_id: UUID,
    request: Request,
    user_ids: UserIDs = Depends(get_current_user),
) -> EventSourceResponse:
    """Replay and stream one user-owned initial sync from an optional event cursor."""
    service = await _authorize_initial_sync(user_ids, sync_id)
    resume_from_id = request.headers.get("last-event-id")

    async def event_generator():
        """Stop delivery on browser disconnect while leaving workers untouched."""
        try:
            async for event in service.stream_events(resume_from_id=resume_from_id):
                if await request.is_disconnected():
                    break
                yield event
        finally:
            await service.close()

    return EventSourceResponse(event_generator())


def _mark_capability_state_disconnected(state: Any) -> None:
    state.enabled = False
    state.last_sync_status = "disconnected"
    state.last_sync_error = "Integration disconnected by user"


async def _disable_capability_states(
    service: IntegrationService,
    integration_id: UUID,
    integration: object | None = None,
) -> None:
    if hasattr(service.db, "execute"):
        stmt = select(IntegrationSyncState).where(
            col(IntegrationSyncState.integration_id) == integration_id,
            col(IntegrationSyncState.organisation_id) == service.user_ids.organisation_id,
        )
        no_autoflush = getattr(service.db, "no_autoflush", None)
        if no_autoflush is None:
            result = await service.db.execute(stmt)
        else:
            with no_autoflush:
                result = await service.db.execute(stmt)
        for state in result.scalars().all():
            _mark_capability_state_disconnected(state)
            service.db.add(state)
        return

    attached_states = getattr(integration, "sync_states", None)
    if attached_states:
        for state in attached_states:
            _mark_capability_state_disconnected(state)
            service.db.add(state)


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

        if integration.is_active is False and integration.requires_reauth:
            integration.requires_reauth = False
            integration.last_sync_error = "Integration removed by user"
            await _disable_capability_states(service, integration_id, integration)
            service.db.add(integration)
            await service.db.commit()

            logger.info(
                "Removed expired inactive integration %s from action-required state for org %s",
                integration_id,
                service.user_ids.organisation_id,
            )
            return Response.success_response(
                message="Integration removed from action-required state successfully", data=None
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
        await _disable_capability_states(service, integration_id, integration)
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
