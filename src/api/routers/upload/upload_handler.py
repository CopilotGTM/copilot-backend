from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, status, Body
from sqlmodel import Session

from src.infra.utils.logger import get_logger
from src.api.schemas.upload_models import FinalizeRequest, PresignRequest, PresignResponse, UploadedAttachmentResponse
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.api.utils.http_response import Response
from src.api.schemas.base_models import GenericResponse
from src.service.upload_service import UploadService, UploadServiceError
from src.api.dependencies.organisation_dependencies import get_current_user, UserIDs, get_org_session
from src.celery_app import celery

logger = get_logger("upload_handler")
upload_router = APIRouter()


def get_upload_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: Session = Depends(get_org_session),
) -> UploadService:
    """Dependency to create an instance of UploadService, configured for the current organisation."""
    return UploadService(org_session, user_ids.organisation_id)


@upload_router.post(
    "/presign",
    response_model=GenericResponse[PresignResponse],
    tags=["file_upload"],
    summary="Get SAS URL for direct client upload",
)
async def presign_upload(
    payload: PresignRequest = Body(...),
    service: UploadService = Depends(get_upload_service),
    user_ids: UserIDs = Depends(get_current_user),
):
    """
    Generate a presigned URL for direct client-side file upload.

    This endpoint validates the request against file size and MIME type constraints
    before creating an attachment record and generating a secure upload URL.
    """
    try:
        attachment, upload_url = await service.presign_upload(payload, user_ids.user_id)
        resp = PresignResponse(
            attachment_id=attachment.attachment_id,
            upload_url=str(upload_url),
            blob_path=attachment.storage_path,
            expires_in_seconds=15 * 60,  # 15 minutes
        )
        return Response.success_response(message="Presigned upload URL created", data=resp)
    except UploadServiceError as e:
        return Response.error_response(status_code=e.status_code, message=e.message)
    except ValueError as e:
        logger.warning("Presign request validation failed for user %s: %s", user_ids.user_id, e)
        return Response.error_response(status_code=status.HTTP_400_BAD_REQUEST, message=str(e))
    except Exception as e:
        logger.error("An unexpected error occurred during presign_upload for user %s: %s", user_ids.user_id, e, exc_info=True)
        return Response.internal_server_error_response(message="An unexpected error occurred")


@upload_router.post(
    "/finalize",
    response_model=GenericResponse[UploadedAttachmentResponse],
    tags=["file_upload"],
    summary="Finalize a previously presigned upload",
)
async def finalize_upload(
    payload: FinalizeRequest = Body(...),
    service: UploadService = Depends(get_upload_service),
    user_ids: UserIDs = Depends(get_current_user),
):
    """
    Finalize a presigned upload by verifying checksum and updating metadata.

    This endpoint should be called after the client successfully uploads the file.
    It verifies the file's integrity via checksum and triggers post-processing
    tasks like virus scanning.
    """
    try:
        attachment = await service.finalize_upload(payload, user_ids.user_id)
        storage = AzureOrganisationBlobStorage(user_ids.organisation_id)
        read_url = await storage.generate_read_sas(
            "attachments", attachment.storage_path, minutes=120, download_filename=attachment.filename
        )

        # Enqueue async image processing (checksum + thumbnails if applicable)
        try:
            celery.send_task(
                "attachments.process_image",
                args=[str(user_ids.organisation_id), str(attachment.attachment_id)],
            )
        except Exception as task_err:
            logger.error(
                "Failed to enqueue processing for attachment %s: %s",
                attachment.attachment_id,
                task_err,
            )

        # If thumbnails already generated (e.g., fast processing for tiny images), include temporary SAS URLs
        thumb_small_url = None
        thumb_medium_url = None
        if getattr(attachment, "thumbnail_small_path", None):
            try:
                thumb_small_url = await storage.generate_read_sas(
                    "attachments", attachment.thumbnail_small_path, minutes=120
                )
            except Exception:
                pass
        if getattr(attachment, "thumbnail_medium_path", None):
            try:
                thumb_medium_url = await storage.generate_read_sas(
                    "attachments", attachment.thumbnail_medium_path, minutes=120
                )
            except Exception:
                pass

        resp = UploadedAttachmentResponse(
            attachment_id=attachment.attachment_id,
            filename=attachment.filename,
            url=str(read_url),
            mimetype=attachment.mimetype,
            size_bytes=attachment.size_bytes,
            uploaded_at=attachment.created_at,
            checksum=attachment.checksum,
            virus_scan_status=attachment.virus_scan_status,
            metadata=attachment.attachment_metadata,
            processing_status=getattr(attachment, "processing_status", "pending"),
            thumbnail_small_url=thumb_small_url,
            thumbnail_medium_url=thumb_medium_url,
        )
        return Response.success_response(message="Attachment finalized successfully", data=resp)
    except UploadServiceError as e:
        return Response.error_response(status_code=e.status_code, message=e.message)
    except Exception as e:
        logger.error(
            "An unexpected error occurred during finalize_upload for attachment %s: %s",
            payload.attachment_id,
            e,
            exc_info=True,
        )
        return Response.internal_server_error_response(message="An unexpected error occurred")


@upload_router.get(
    "/attachment/{attachment_id}",
    response_model=GenericResponse[UploadedAttachmentResponse],
    tags=["file_upload"],
    summary="Get attachment status and details",
)
async def get_attachment_status(
    attachment_id: UUID,
    service: UploadService = Depends(get_upload_service),
    user_ids: UserIDs = Depends(get_current_user),
):
    """
    Poll this endpoint to get the current status of an attachment.

    Returns the attachment's processing status, and thumbnail URLs if processing is complete.
    """
    try:
        attachment = await service.get_attachment_details(attachment_id, user_ids.user_id)
        storage = AzureOrganisationBlobStorage(user_ids.organisation_id)
        
        read_url = await storage.generate_read_sas(
            "attachments", attachment.storage_path, minutes=120, download_filename=attachment.filename
        )

        thumb_small_url = None
        thumb_medium_url = None
        if getattr(attachment, "thumbnail_small_path", None):
            thumb_small_url = await storage.generate_read_sas(
                "attachments", attachment.thumbnail_small_path, minutes=120
            )
        if getattr(attachment, "thumbnail_medium_path", None):
            thumb_medium_url = await storage.generate_read_sas(
                "attachments", attachment.thumbnail_medium_path, minutes=120
            )

        resp = UploadedAttachmentResponse(
            attachment_id=attachment.attachment_id,
            filename=attachment.filename,
            url=str(read_url),
            mimetype=attachment.mimetype,
            size_bytes=attachment.size_bytes,
            uploaded_at=attachment.created_at,
            checksum=attachment.checksum,
            virus_scan_status=attachment.virus_scan_status,
            metadata=attachment.attachment_metadata,
            processing_status=getattr(attachment, "processing_status", "pending"),
            thumbnail_small_url=thumb_small_url,
            thumbnail_medium_url=thumb_medium_url,
        )
        return Response.success_response(message="Attachment status retrieved", data=resp)
    except UploadServiceError as e:
        return Response.error_response(status_code=e.status_code, message=e.message)
    except Exception as e:
        logger.error(
            "An unexpected error occurred during get_attachment_status for attachment %s: %s",
            attachment_id,
            e,
            exc_info=True,
        )
        return Response.internal_server_error_response(message="An unexpected error occurred")
