"""CRM user selection API handler."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi import Response as FastAPIResponse
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
)
from src.api.dependencies.rate_limit import rate_limit_user_read
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.crm_user_models import CRMUserListItem, CRMUsersPage
from src.api.utils.http_response import Response
from src.infra.utils.logger import get_logger
from src.service.crm.access.crm_user_service import (
    CRMUserService,
    CRMUserServiceError,
    build_user_display_name,
)

logger = get_logger(__name__)

_UNEXPECTED_ERR_MSG = "An unexpected error occurred while processing your request"

crm_users_router = APIRouter()


async def get_crm_user_service(
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_master_async_session),
):
    service = CRMUserService(session, user_ids.organisation_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("CRMUserService close failed", exc_info=True)


@crm_users_router.get(
    "/users",
    response_model=GenericResponse[CRMUsersPage],
    summary="List organisation users for CRM owner/team selection",
)
async def list_crm_users(
    response: FastAPIResponse,
    limit: int = Query(50, ge=1, le=100, description="Maximum users per page"),
    cursor: str | None = Query(None, description="Pagination cursor"),
    q: str | None = Query(None, description="Search by name or email"),
    service: CRMUserService = Depends(get_crm_user_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[CRMUsersPage]:
    """List app users for the organisation (owner/team selection)."""
    try:
        users, next_cursor = await service.list_users(limit=limit, cursor=cursor, q=q)
        items = [
            CRMUserListItem(
                user_id=user.user_id,
                name=build_user_display_name(user),
                email=str(user.email) if user.email else None,
                role=user.user_role.value if hasattr(user.user_role, "value") else None,
                job_title=user.job_title,
                organisation_role=user.organisation_role,
                status=user.status.value if hasattr(user.status, "value") else None,
            )
            for user in users
        ]
        payload = CRMUsersPage(items=items, cursor=next_cursor)
        return Response.success_response(message="Users retrieved successfully", data=payload)
    except CRMUserServiceError as exc:
        logger.warning("CRM user list error: %s", exc.message)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        logger.error("Unexpected error listing CRM users: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )
