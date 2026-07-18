"""Admin-only listing endpoint for attachments sent through chat."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi import Response as FastAPIResponse
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.dependencies.organisation_dependencies import get_org_async_session
from src.api.dependencies.rate_limit import rate_limit_user_read
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.upload_models import ChatAttachmentsPage
from src.api.utils.http_response import Response
from src.domain.models.master_tables import User, UserAppRole
from src.infra.auth.user_auth import user_auth
from src.infra.utils.logger import get_logger
from src.service.chat.chat_attachment_catalog_service import ChatAttachmentCatalogService

logger = get_logger("chat_attachment_catalog")
catalog_router = APIRouter()


def require_organisation_admin(current_user: User = Depends(user_auth)) -> User:
    """Allow only application admins to enumerate organisation-wide attachments."""
    if current_user.user_role != UserAppRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Organisation admin access required",
        )
    return current_user


async def get_attachment_catalog_service(
    current_user: User = Depends(require_organisation_admin),
    org_session: AsyncSession = Depends(get_org_async_session),
    master_session: AsyncSession = Depends(get_master_async_session),
):
    """Yield an organisation-scoped catalogue service for one admin request."""
    service = ChatAttachmentCatalogService(
        org_session,
        master_session,
        current_user.organisation_id,
    )
    try:
        yield service
    finally:
        await service.close()


@catalog_router.get(
    "/attachments",
    response_model=GenericResponse[ChatAttachmentsPage],
    tags=["chat_attachments"],
    summary="List sent chat attachments for organisation admins",
)
async def list_chat_attachments(
    response: FastAPIResponse,
    media_type: Literal["audio"] = Query("audio"),
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(None, description="Opaque cursor from the previous page"),
    service: ChatAttachmentCatalogService = Depends(get_attachment_catalog_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[ChatAttachmentsPage]:
    """List playable sent audio using persisted metadata and short-lived URLs."""
    try:
        items, next_cursor = await service.list_attachments(
            media_type=media_type,
            limit=limit,
            cursor=cursor,
        )
        return Response.success_response(
            "Chat attachments listed",
            ChatAttachmentsPage(items=items, cursor=next_cursor),
        )
    except (ValueError, TypeError):
        return Response.bad_request_error_response("Invalid cursor", response=response)
    except Exception as exc:
        logger.error("List chat attachments failed: %s", exc, exc_info=True)
        return Response.internal_server_error_response(
            "Unable to list chat attachments",
            response=response,
        )
