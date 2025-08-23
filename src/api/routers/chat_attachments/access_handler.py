from __future__ import annotations

from uuid import UUID
from fastapi import APIRouter, Depends, Query
from sqlmodel import Session

from src.api.dependencies.organisation_dependencies import get_current_user, UserIDs, get_org_session
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.upload_models import AttachmentDownloadResponse
from src.api.utils.http_response import Response
from src.service.chat.chat_attachments_service import UploadService, UploadServiceError
from src.infra.utils.logger import get_logger

logger = get_logger("attachment_download_handler")

download_router = APIRouter()


async def get_upload_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: Session = Depends(get_org_session),
):
    service = UploadService(org_session, user_ids.organisation_id)
    try:
        yield service
    finally:
        try:
            await service.close()  # type: ignore[attr-defined]
        except Exception:
            logger.debug("UploadService close failed (download handler)", exc_info=True)


@download_router.get(
    "/attachment/{attachment_id}/download",
    response_model=GenericResponse[AttachmentDownloadResponse],
    tags=["file_download"],
    summary="Generate a download URL for an attachment",
)
async def download_attachment(
    attachment_id: UUID,
    expires_minutes: int = Query(30, ge=1, le=120),
    service: UploadService = Depends(get_upload_service),
    user_ids: UserIDs = Depends(get_current_user),
):
    try:
        url = await service.generate_download_url(attachment_id, user_ids.user_id, minutes=expires_minutes)
        resp = AttachmentDownloadResponse(
            attachment_id=attachment_id,
            download_url=url,
            expires_in_seconds=expires_minutes * 60,
        )
        return Response.success_response("Download URL generated", resp)
    except UploadServiceError as e:
        return Response.error_response(status_code=e.status_code, message=e.message)
    except Exception as e:  # pragma: no cover
        logger.error("Unexpected error generating attachment download URL: %s", e, exc_info=True)
        return Response.internal_server_error_response("An unexpected error occurred")
