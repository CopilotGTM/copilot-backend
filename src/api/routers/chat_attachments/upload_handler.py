from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Body, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    acquire_org_async_session,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.upload_models import (
    AttachmentBatchFinalizeRequest,
    AttachmentBatchFinalizeResponse,
    AttachmentBatchFinalizeResult,
    AttachmentBatchPresignRequest,
    AttachmentBatchPresignResponse,
    AttachmentPresignFileResponse,
    FinalizeRequest,
    PresignRequest,
    PresignResponse,
    UploadedAttachmentResponse,
)
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables.chat_message_models import AttachmentStatus
from src.infra.utils.logger import get_logger
from src.service.chat.chat_attachments_service import UploadService, UploadServiceError
from src.service.chat.chat_attachments_sse_service import (
    AttachmentSSEService,
)
from src.workers.chat_worker.celery_app import app as chat_app
from sse_starlette.sse import EventSourceResponse

# Reused literals / constants
UNEXPECTED_ERROR_MSG = "An unexpected error occurred"

logger = get_logger("upload_handler")
upload_router = APIRouter()


def _get_status_str(status_enum):
    """Safely get the string value of a status enum."""
    if status_enum is None:
        return None
    return getattr(status_enum, "value", str(status_enum))


async def get_upload_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
):
    """FastAPI dependency returning an UploadService bound to caller's organisation.

    Implemented as async generator to guarantee cleanup of underlying async
    Azure clients (mirrors knowledge base upload handler pattern) and avoid
    'Unclosed client session' warnings during high concurrency.
    """
    service = UploadService(org_session, user_ids.organisation_id, user_ids.user_id)
    try:
        yield service
    finally:
        try:
            await service.close()  # type: ignore[attr-defined]
        except Exception:
            logger.debug("UploadService close failed", exc_info=True)


@upload_router.post(
    "/presign/batch",
    response_model=GenericResponse[AttachmentBatchPresignResponse],
    tags=["file_upload"],
    summary="Batch presign multiple attachments",
)
async def presign_batch(
    payload: AttachmentBatchPresignRequest = Body(...),
    service: UploadService = Depends(get_upload_service),
    _rl: None = Depends(rate_limit_user_write),
):
    try:
        expiry_minutes = 15
        created, errors = await service.presign_batch(payload, expiry_minutes)

        success_entries = [
            AttachmentPresignFileResponse(
                attachment_id=att.attachment_id,
                filename=att.filename,
                mimetype=att.mimetype,
                upload_url=url,
                size_bytes=getattr(att, "size_bytes", None),
                success=True,
                error_message=None,
                expires_in_seconds=expiry_minutes * 60,
                status=_get_status_str(AttachmentStatus.PENDING_UPLOAD),
            )
            for att, url in created
        ]

        failed_entries: list[AttachmentPresignFileResponse] = []
        for err in errors:
            failed_entries.append(
                AttachmentPresignFileResponse(
                    attachment_id=None,
                    filename=err.get("filename", ""),
                    mimetype=None,
                    upload_url=None,
                    success=False,
                    error_message=err.get("error_message"),
                    size_bytes=None,
                    expires_in_seconds=0,
                    status=_get_status_str(AttachmentStatus.FAILED),
                )
            )
        all_files = success_entries + failed_entries
        resp = AttachmentBatchPresignResponse(files=all_files, errors=errors)
        message = "Batch presign completed"
        if failed_entries and not success_entries:
            return Response.error_response(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                message="All presign operations failed",
                data=resp,
            )
        if failed_entries:
            message = f"Batch presign completed with {len(failed_entries)} errors"
        return Response.success_response(message=message, data=resp)
    except UploadServiceError as e:
        return Response.error_response(status_code=e.status_code, message=e.message)
    except ValueError as e:
        return Response.error_response(status_code=status.HTTP_400_BAD_REQUEST, message=str(e))
    except Exception:
        logger.error("Unexpected error in attachment batch presign", exc_info=True)
        return Response.internal_server_error_response(UNEXPECTED_ERROR_MSG)


@upload_router.post(
    "/finalize/batch",
    response_model=GenericResponse[AttachmentBatchFinalizeResponse],
    tags=["file_upload"],
    summary="Batch finalize multiple attachments",
)
async def finalize_batch(
    payload: AttachmentBatchFinalizeRequest = Body(...),
    service: UploadService = Depends(get_upload_service),
    _rl: None = Depends(rate_limit_user_write),
):
    results: list[AttachmentBatchFinalizeResult] = []
    errors: list[dict] = []
    try:
        finalized, finalize_errors = await service.finalize_batch(payload)
        errors.extend(finalize_errors)
        # Enqueue processing task per successfully finalized attachment
        for att in finalized:
            try:
                chat_app.send_task(
                    "attachments.post_upload",
                    args=[str(service.organisation_id), str(att.attachment_id)],
                    queue="attachments",
                    routing_key="attachments",
                )
            except Exception as task_err:
                logger.error(
                    "Failed to enqueue processing for attachment %s: %s",
                    att.attachment_id,
                    task_err,
                )
            results.append(
                AttachmentBatchFinalizeResult(
                    attachment_id=att.attachment_id,
                    status=_get_status_str(getattr(att, "status", None)),
                    error=getattr(att, "processing_error", None),
                    updated_at=getattr(att, "updated_at", getattr(att, "created_at", None)),
                )
            )
        resp = AttachmentBatchFinalizeResponse(results=results, errors=errors)
        if results:
            return Response.success_response(message="Batch finalize completed", data=resp)
        return Response.error_response(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            message="All finalize operations failed",
            data=resp,
        )
    except UploadServiceError as e:
        return Response.error_response(status_code=e.status_code, message=e.message)
    except Exception:
        logger.error("Unexpected error in attachment batch finalize", exc_info=True)
        return Response.internal_server_error_response(UNEXPECTED_ERROR_MSG)


@upload_router.get(
    "/attachments/stream",
    tags=["file_upload"],
    summary="Stream attachment processing updates via SSE",
)
async def stream_attachments(
    user_ids: UserIDs = Depends(get_current_user),
    attachment_ids: list[UUID] | None = Query(
        default=None,
        description="Optional list of attachment UUIDs to monitor. If omitted, monitors all active sources.",
    ),
):
    logger.info(
        "Starting attachment SSE stream for user %s ids=%s",
        user_ids.user_id,
        attachment_ids,
    )
    explicit_ids: set[UUID] | None = set(attachment_ids) if attachment_ids else None
    logger.info("Attachment SSE explicit IDs: %s", explicit_ids)

    async def event_generator():
        # Manual control of session lifetime (mirror knowledge base SSE pattern)
        sess_gen = acquire_org_async_session(user_ids.organisation_id)
        session = await anext(sess_gen)
        service = AttachmentSSEService(session, user_ids.organisation_id, user_ids.user_id)
        try:
            async for evt in service.stream_events(
                explicit_ids=explicit_ids,
                timeout_seconds=1800,
                heartbeat_interval=25.0,
            ):
                yield evt
        finally:
            try:
                await service.close()
            except Exception:
                logger.debug("SSE service close failed", exc_info=True)
            try:
                await sess_gen.aclose()
            except Exception:
                logger.debug("SSE session generator finalization failed", exc_info=True)

    return EventSourceResponse(event_generator())


@upload_router.post(
    "/presign",
    response_model=GenericResponse[PresignResponse],
    tags=["file_upload"],
    summary="Get SAS URL for direct client upload (minimal response)",
)
async def presign_upload(
    payload: PresignRequest = Body(...),
    service: UploadService = Depends(get_upload_service),
    _rl: None = Depends(rate_limit_user_write),
):
    """
    Generate a presigned URL for direct client-side file upload.

    This endpoint validates the request against file size and MIME type constraints
    before creating an attachment record and generating a secure upload URL.
    """
    try:
        expiry_minutes = 15
        attachment, upload_url = await service.presign_upload(payload)
        resp = PresignResponse(
            attachment_id=attachment.attachment_id,
            filename=attachment.filename,
            mimetype=attachment.mimetype,
            upload_url=str(upload_url),
            size_bytes=attachment.size_bytes,
            expires_in_seconds=expiry_minutes * 60,
            status=_get_status_str(AttachmentStatus.PENDING_UPLOAD),
            success=True,
            error_message=None,
        )
        return Response.success_response(message="Presigned upload URL created", data=resp)
    except UploadServiceError as e:
        return Response.error_response(status_code=e.status_code, message=e.message)
    except ValueError as e:
        logger.warning("Presign request validation failed for user %s: %s", service.user_id, e)
        return Response.error_response(status_code=status.HTTP_400_BAD_REQUEST, message=str(e))
    except Exception as e:
        logger.error("Unexpected error in attachment presign: %s", e, exc_info=True)
        return Response.internal_server_error_response(UNEXPECTED_ERROR_MSG)


@upload_router.post(
    "/finalize",
    response_model=GenericResponse[UploadedAttachmentResponse],
    tags=["file_upload"],
    summary="Finalize a previously presigned upload",
)
async def finalize_upload(
    payload: FinalizeRequest = Body(...),
    service: UploadService = Depends(get_upload_service),
    _rl: None = Depends(rate_limit_user_write),
):
    """
    Finalize a presigned upload by verifying checksum and updating metadata.

    This endpoint should be called after the client successfully uploads the file.
    It verifies the file's integrity via checksum and triggers post-processing
    tasks like virus scanning.
    """
    try:
        attachment = await service.finalize_upload(payload)

        # Schedule single post‑upload processing task (checksum + thumbnails)
        chat_app.send_task(
            "attachments.post_upload",
            args=[str(service.organisation_id), str(attachment.attachment_id)],
            queue="attachments",
            routing_key="attachments",
        )

        # Only return the minimal finalized attachment payload for chat clients.
        resp = UploadedAttachmentResponse(
            attachment_id=attachment.attachment_id,
            uploaded_at=attachment.created_at,
            status=_get_status_str(attachment.status),
        )
        return Response.success_response(message="Attachment finalized", data=resp)
    except UploadServiceError as e:
        return Response.error_response(status_code=e.status_code, message=e.message)
    except Exception:
        logger.error(
            "Unexpected error finalizing attachment %s",
            payload.attachment_id,
            exc_info=True,
        )
        return Response.internal_server_error_response(UNEXPECTED_ERROR_MSG)


@upload_router.get(
    "/attachment/{attachment_id}",
    response_model=GenericResponse[UploadedAttachmentResponse],
    tags=["file_upload"],
    summary="Get attachment status and details",
)
async def get_attachment_status(
    attachment_id: UUID,
    service: UploadService = Depends(get_upload_service),
):
    """
    Poll this endpoint to get the current status of an attachment.

    Returns the attachment's processing status, and thumbnail URLs if processing is complete.
    """
    try:
        attachment = await service.get_attachment_details(attachment_id)
        if not attachment:
            return Response.error_response(
                status_code=status.HTTP_404_NOT_FOUND,
                message=f"Attachment {attachment_id} not found",
            )

        # Keep get endpoint minimal — only return id, timestamp and logical status.
        resp = UploadedAttachmentResponse(
            attachment_id=attachment.attachment_id,
            uploaded_at=attachment.created_at,
            status=_get_status_str(getattr(attachment, "status", None)),
        )
        return Response.success_response(message="Attachment status retrieved", data=resp)
    except UploadServiceError as e:
        return Response.error_response(status_code=e.status_code, message=e.message)
    except Exception:
        logger.error("Unexpected error retrieving attachment %s", attachment_id, exc_info=True)
        return Response.internal_server_error_response(UNEXPECTED_ERROR_MSG)
