from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, status, Body
from sqlmodel import Session

from src.infra.utils.logger import get_logger
from src.api.utils.http_response import Response
from src.api.schemas.base_models import GenericResponse
from src.api.dependencies.organisation_dependencies import get_current_user, UserIDs, get_org_session
from src.api.schemas.knowledge_upload_models import (
    KnowledgeBatchPresignRequest,
    KnowledgeBatchPresignResponse,
    KnowledgePresignFileResponse,
    KnowledgeFinalizeRequest,
    KnowledgeBatchFinalizeRequest,
    KnowledgeSourceResponse,
)
from src.service.knowledge_upload_service import (
    KnowledgeUploadService,
    KnowledgeUploadServiceError,
)
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage

logger = get_logger("knowledge_upload_handler")
upload_router = APIRouter()


def get_knowledge_upload_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: Session = Depends(get_org_session),
) -> KnowledgeUploadService:
    return KnowledgeUploadService(org_session, user_ids.organisation_id)


@upload_router.post(
    "/presign/batch",
    response_model=GenericResponse[KnowledgeBatchPresignResponse],
    tags=["knowledge_upload"],
    summary="Batch presign multiple knowledge source uploads",
)
async def presign_batch(
    payload: KnowledgeBatchPresignRequest = Body(...),
    service: KnowledgeUploadService = Depends(get_knowledge_upload_service),
    user_ids: UserIDs = Depends(get_current_user),
):
    try:
        created, errors = await service.presign_batch(payload, user_ids.user_id)
        def _status_val(s):
            return s.value if hasattr(s, "value") else str(s)
        
        # Create response for successful files
        success_files = [
            KnowledgePresignFileResponse(
                source_id=src.source_id,
                filename=src.filename or "",
                mimetype=src.mimetype or "",
                size_bytes=src.size_bytes or 0,
                upload_url=upload_url,
                blob_path=src.storage_path or "",
                expires_in_seconds=15 * 60,
                status=_status_val(src.status),
                success=True,
            )
            for src, upload_url in created
        ]
        
        # Create response for failed files with error details
        failed_files = []
        for error in errors:
            # Generate a placeholder source_id for failed files if not provided
            source_id = error.get("source_id", "00000000-0000-0000-0000-000000000000")
            failed_files.append(
                KnowledgePresignFileResponse(
                    source_id=source_id,
                    filename=error.get("filename", ""),
                    mimetype="",
                    size_bytes=0,
                    upload_url=None,
                    blob_path="",
                    expires_in_seconds=0,
                    status="failed",
                    error_message=error.get("error_message", error.get("reason", "Unknown error")),
                    success=False,
                )
            )
        
        # Combine successful and failed files
        all_files = success_files + failed_files
        resp = KnowledgeBatchPresignResponse(files=all_files, errors=errors)
        
        message = "Batch presign completed"
        if failed_files and not success_files:
            return Response.error_response(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                message="All presign operations failed",
                data=resp,
            )
        elif failed_files:
            message = f"Batch presign completed with {len(failed_files)} errors"
            
        return Response.success_response(message=message, data=resp)
    except ValueError as e:
        return Response.error_response(status_code=status.HTTP_400_BAD_REQUEST, message=str(e))
    except Exception as e:
        logger.error("Unexpected error in knowledge presign batch: %s", e, exc_info=True)
        return Response.internal_server_error_response("An unexpected error occurred")


@upload_router.post(
    "/presign",
    response_model=GenericResponse[KnowledgeBatchPresignResponse],
    tags=["knowledge_upload"],
    summary="Single-file presign convenience endpoint",
)
async def presign_single(
    payload: KnowledgeBatchPresignRequest,
    service: KnowledgeUploadService = Depends(get_knowledge_upload_service),
    user_ids: UserIDs = Depends(get_current_user),
):
    if len(payload.files) != 1:
        return Response.error_response(
            status_code=status.HTTP_400_BAD_REQUEST, message="Exactly one file expected for this endpoint."
        )
    return await presign_batch(payload, service, user_ids)


@upload_router.post(
    "/finalize",
    response_model=GenericResponse[KnowledgeSourceResponse],
    tags=["knowledge_upload"],
    summary="Finalize a knowledge source upload",
)
async def finalize_source(
    payload: KnowledgeFinalizeRequest = Body(...),
    service: KnowledgeUploadService = Depends(get_knowledge_upload_service),
    user_ids: UserIDs = Depends(get_current_user),
):
    try:
        source = await service.finalize_source(payload, user_ids.user_id)
        storage = AzureOrganisationBlobStorage(user_ids.organisation_id)
        read_url = None
        if source.storage_path:
            try:
                read_url = await storage.generate_read_sas(
                    source.storage_path, minutes=120, download_filename=source.filename
                )
            except Exception:
                pass
        status_val = source.status.value if hasattr(source.status, "value") else str(source.status)
        resp = KnowledgeSourceResponse(
            source_id=source.source_id,
            filename=source.filename,
            mimetype=source.mimetype,
            size_bytes=source.size_bytes,
            status=status_val,
            created_at=source.created_at,
            updated_at=source.updated_at,
            error_message=source.error_message,
            storage_path=source.storage_path,
            read_url=read_url,
            filters=source.filters or {},
        )
        return Response.success_response(message="Source finalized", data=resp)
    except KnowledgeUploadServiceError as e:
        return Response.error_response(status_code=e.status_code, message=e.message)
    except Exception as e:
        logger.error("Unexpected error finalizing knowledge source %s", payload.source_id, exc_info=True)
        return Response.internal_server_error_response("An unexpected error occurred")


@upload_router.post(
    "/finalize/batch",
    response_model=GenericResponse[dict],
    tags=["knowledge_upload"],
    summary="Batch finalize multiple knowledge source uploads",
)
async def finalize_batch(
    payload: KnowledgeBatchFinalizeRequest = Body(...),
    service: KnowledgeUploadService = Depends(get_knowledge_upload_service),
    user_ids: UserIDs = Depends(get_current_user),
):
    results = []
    errors = []
    for item in payload.files:
        try:
            src = await service.finalize_source(item, user_ids.user_id)
        except KnowledgeUploadServiceError as e:
            errors.append({"source_id": str(item.source_id), "reason": e.message})
            continue
        except Exception:
            errors.append({"source_id": str(item.source_id), "reason": "unexpected_error"})
            continue
        status_val = src.status.value if hasattr(src.status, "value") else str(src.status)
        results.append(
            {
                "source_id": str(src.source_id),
                "status": status_val,
                "error": src.error_message,
            }
        )
    data = {"results": results, "errors": errors}
    if results:
        return Response.success_response(message="Batch finalize completed", data=data)
    return Response.error_response(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, message="All finalize operations failed", data=data)


@upload_router.get(
    "/source/{source_id}",
    response_model=GenericResponse[KnowledgeSourceResponse],
    tags=["knowledge_upload"],
    summary="Get knowledge source status",
)
async def get_source(
    source_id: UUID,
    service: KnowledgeUploadService = Depends(get_knowledge_upload_service),
    user_ids: UserIDs = Depends(get_current_user),
):
    try:
        source = await service.get_source(source_id, user_ids.user_id)
        storage = AzureOrganisationBlobStorage(user_ids.organisation_id)
        read_url = None
        if source.storage_path:
            try:
                read_url = await storage.generate_read_sas(
                    source.storage_path, minutes=120, download_filename=source.filename
                )
            except Exception:
                pass
        status_val = source.status.value if hasattr(source.status, "value") else str(source.status)
        resp = KnowledgeSourceResponse(
            source_id=source.source_id,
            filename=source.filename,
            mimetype=source.mimetype,
            size_bytes=source.size_bytes,
            status=status_val,
            created_at=source.created_at,
            updated_at=source.updated_at,
            error_message=source.error_message,
            storage_path=source.storage_path,
            read_url=read_url,
            filters=source.filters or {},
        )
        return Response.success_response(message="Source status retrieved", data=resp)
    except KnowledgeUploadServiceError as e:
        return Response.error_response(status_code=e.status_code, message=e.message)
    except Exception:
        return Response.internal_server_error_response("An unexpected error occurred")
