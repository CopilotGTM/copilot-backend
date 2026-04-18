from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends
from fastapi import Response as FastAPIResponse
from src.api.dependencies.organisation_dependencies import UserIDs, get_current_user
from src.api.dependencies.rate_limit import rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.utils.http_response import Response
from src.domain.models.user_models import (
    UserRealtimeCallAttachRequest,
    UserRealtimeCallAttachResponse,
    UserRealtimeSessionRequest,
    UserRealtimeSessionResponse,
)
from src.infra.utils.logger import get_logger
from src.service.users.realtime_session_service import (
    RealtimeSessionService,
    RealtimeSessionServiceError,
)

logger = get_logger(__name__)
router = APIRouter()


async def get_realtime_session_service(
    user_ids: UserIDs = Depends(get_current_user),
) -> AsyncIterator[RealtimeSessionService]:
    """Yield a request-scoped realtime session service."""
    service = RealtimeSessionService(user_ids)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("RealtimeSessionService close failed", exc_info=True)


@router.post(
    "/session",
    response_model=GenericResponse[UserRealtimeSessionResponse],
    tags=["user_realtime"],
)
async def create_realtime_session(
    session_request: UserRealtimeSessionRequest,
    response: FastAPIResponse,
    service: RealtimeSessionService = Depends(get_realtime_session_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[UserRealtimeSessionResponse]:
    """Create a short-lived Azure realtime client secret for the mobile app."""
    try:
        session_response = await service.create_session(session_request)
        return Response.success_response(
            message="Realtime session created successfully.",
            data=session_response,
            response=response,
        )
    except RealtimeSessionServiceError as exc:
        logger.warning(
            "Realtime session bootstrap failed",
            extra={
                "event": "realtime_bootstrap_failed",
                "status_code": exc.status_code,
                "error_message": exc.message,
            },
        )
        return Response.error_response(
            status_code=exc.status_code,
            message=exc.message,
            response=response,
        )
    except Exception:
        logger.exception("Unexpected realtime session bootstrap failure")
        return Response.internal_server_error_response(
            message="An unexpected error occurred during realtime session creation.",
            response=response,
        )


@router.post(
    "/calls/attach",
    response_model=GenericResponse[UserRealtimeCallAttachResponse],
    tags=["user_realtime"],
)
async def attach_realtime_call(
    attach_request: UserRealtimeCallAttachRequest,
    response: FastAPIResponse,
    service: RealtimeSessionService = Depends(get_realtime_session_service),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[UserRealtimeCallAttachResponse]:
    """Attach backend sideband control to a live realtime call."""
    try:
        attach_response = await service.attach_call(attach_request)
        return Response.success_response(
            message="Realtime call attached successfully.",
            data=attach_response,
            response=response,
        )
    except RealtimeSessionServiceError as exc:
        logger.warning(
            "Realtime call attach failed",
            extra={
                "event": "realtime_call_attach_failed",
                "status_code": exc.status_code,
                "error_message": exc.message,
            },
        )
        return Response.error_response(
            status_code=exc.status_code,
            message=exc.message,
            response=response,
        )
    except Exception:
        logger.exception("Unexpected realtime call attach failure")
        return Response.internal_server_error_response(
            message="An unexpected error occurred during realtime call attachment.",
            response=response,
        )
