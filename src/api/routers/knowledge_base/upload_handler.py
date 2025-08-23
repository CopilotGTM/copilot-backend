"""Knowledge base upload API router.

This module exposes endpoints to presign and finalize knowledge source
uploads. It delegates core business logic to
`KnowledgeUploadService` and enqueues background Celery tasks for
post‑upload processing (checksum + thumbnail etc.).

All endpoints return a uniform `GenericResponse` envelope.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Body, Depends, status, Query
from sqlmodel import Session
from typing import Optional, Set
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_session,
)
from src.infra.utils.database import get_org_session as raw_get_org_session
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.knowledge_upload_models import (
    KnowledgeBatchFinalizeRequest,
    KnowledgeBatchPresignRequest,
    KnowledgeBatchPresignResponse,
    KnowledgeFinalizeRequest,
    KnowledgePresignFileResponse,
    KnowledgeSourceResponse,
)
from src.api.utils.http_response import Response
from src.celery_app import celery
from src.infra.storage.azure_org_blob_storage import AzureOrganisationBlobStorage
from src.infra.utils.logger import get_logger
from src.service.knowledge_base.knowledge_upload_service import (
    KnowledgeUploadService,
    KnowledgeUploadServiceError,
)
from src.service.knowledge_base.knowledge_sse_service import (
    KnowledgeSSEError,
    KnowledgeSourceSSEService
)
from src.domain.models.organisation_tables.job_models import (
    KnowledgeProcessingJob,
    JobStatus,
)
from sse_starlette.sse import EventSourceResponse

logger = get_logger("knowledge_upload_handler")
upload_router = APIRouter()


async def get_knowledge_upload_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: Session = Depends(get_org_session),
):
    """FastAPI dependency returning a service bound to the caller's org.

    Implemented as an async generator so we can guarantee cleanup of any
    underlying async Azure clients (avoids 'Unclosed client session' warnings).
    """
    service = KnowledgeUploadService(org_session, user_ids.organisation_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:  # Swallow to not mask original response errors
            logger.debug("Service close failed", exc_info=True)


async def get_knowledge_sse_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: Session = Depends(get_org_session),
):
    """FastAPI dependency returning a service bound to the caller's org.

    Implemented as an async generator so we can guarantee cleanup of any
    underlying async Azure clients (avoids 'Unclosed client session' warnings).
    """
    service = KnowledgeSourceSSEService(org_session, user_ids.organisation_id, user_ids.user_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:  # Swallow to not mask original response errors
            logger.debug("Service close failed", exc_info=True)

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
    """Batch presign one or more files.

    Returns signed upload URLs plus per‑file status. Partial success is
    supported; failures are reported with an `errors` list while still
    returning successful signatures.
    """
    try:
        expiry_time = 10
        created, errors = await service.presign_batch(
            payload, user_ids.user_id, expiry_time
        )

        def _status_val(status_obj):
            """Return enum `.value` if present else string representation."""
            return status_obj.value if hasattr(status_obj, "value") else str(status_obj)

        # Build response entries for successfully presigned files
        success_files = [
            KnowledgePresignFileResponse(
                source_id=src.source_id,
                filename=src.filename,
                mimetype=src.mimetype,
                size_bytes=src.size_bytes,
                upload_url=upload_url,
                expires_in_seconds=expiry_time * 60,
                status=_status_val(src.status),
                success=True,
            )
            for src, upload_url in created
        ]

        # Build response entries for failed presign attempts (carry error info)
        failed_files = []
        for error in errors:
            # Provide placeholder UUID if generation failed very early
            source_id = error.get("source_id", "00000000-0000-0000-0000-000000000000")
            failed_files.append(
                KnowledgePresignFileResponse(
                    source_id=source_id,
                    filename=error.get("filename", ""),
                    mimetype="",
                    size_bytes=0,
                    upload_url=None,
                    expires_in_seconds=0,
                    status="failed",
                    error_message=error.get(
                        "error_message", error.get("reason", "Unknown error")
                    ),
                    success=False,
                )
            )

        # Combine successful and failed files for client convenience
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
        return Response.error_response(
            status_code=status.HTTP_400_BAD_REQUEST, message=str(e)
        )
    except Exception as e:
        logger.error(
            "Unexpected error in knowledge presign batch: %s", e, exc_info=True
        )
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
    """Finalize multiple previously presigned & uploaded sources.

    Each file is transitioned into PROCESSING (async task queued). The
    endpoint returns per‑file success or error details; it never aborts
    the whole batch on the first failure.
    """
    results = []
    errors = []
    for item in payload.files:
        try:
            src = await service.finalize_source(item, user_ids.user_id)

            # Create job rows if missing
            session = service.session
            if session:
                session.add(
                    KnowledgeProcessingJob(
                        source_id=str(src.source_id),
                        job_type="post_upload_process",
                        status=JobStatus.PENDING,
                    )
                )
                session.commit()

                session.add(
                    KnowledgeProcessingJob(
                        source_id=str(src.source_id),
                        job_type="document_extraction",
                        status=JobStatus.PENDING,
                    )
                )
                session.commit()

            # Enqueue first phase task
            try:
                celery.send_task(
                    "knowledge.post_upload_process",
                    args=[str(user_ids.organisation_id), str(src.source_id)],
                )
            except Exception as task_err:
                logger.error(
                    "Failed to enqueue knowledge processing for source %s: %s",
                    src.source_id,
                    task_err,
                )
        except KnowledgeUploadServiceError as e:
            errors.append({"source_id": str(item.source_id), "reason": e.message})
            continue
        except Exception:
            errors.append(
                {"source_id": str(item.source_id), "reason": "unexpected_error"}
            )
            continue
        status_val = (
            src.status.value if hasattr(src.status, "value") else str(src.status)
        )
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
    return Response.error_response(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        message="All finalize operations failed",
        data=data,
    )

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
    """Return current state + fresh read SAS (short‑lived) for a source."""
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
            finally:
                try:
                    await storage.close()
                except Exception:
                    logger.debug("Temp storage close failed (get_source)")
        status_val = (
            source.status.value
            if hasattr(source.status, "value")
            else str(source.status)
        )
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
            checksum=getattr(source, "checksum", None),
            thumbnail_small_path=getattr(source, "thumbnail_small_path", None),
            thumbnail_medium_path=getattr(source, "thumbnail_medium_path", None),
        )
        return Response.success_response(message="Source status retrieved", data=resp)
    except KnowledgeUploadServiceError as e:
        return Response.error_response(status_code=e.status_code, message=e.message)
    except Exception:
        return Response.internal_server_error_response("An unexpected error occurred")


@upload_router.get(
    "/sources/stream",
    tags=["knowledge_upload"],
    summary="Stream knowledge source status updates via SSE with Redis pub/sub",
)
async def stream_sources(
    user_ids: UserIDs = Depends(get_current_user),
    source_ids: list[UUID] | None = Query(
        default=None,
        description="Optional list of source UUIDs to monitor. If omitted, monitors all active sources.",
    ),
):
    """Real-time SSE stream for knowledge source status using Redis pub/sub.

    Important: We manually manage the organisation session so its search_path
    remains set for the duration of the SSE stream (FastAPI would otherwise
    finalize dependency scopes after returning EventSourceResponse, resetting
    search_path to public and causing missing tenant table errors).
    """
    logger.info(
        "Starting SSE stream for user %s with source_ids=%s", user_ids.user_id, source_ids
    )

    explicit_ids: Optional[Set[UUID]] = set(source_ids) if source_ids else None

    async def event_generator():
        # Acquire tenant session generator (keeps search_path active until we close)
        sess_gen = raw_get_org_session(str(user_ids.organisation_id))
        session = next(sess_gen)
        service = KnowledgeSourceSSEService(
            session, 
            user_ids.organisation_id, 
            user_ids.user_id
        )
        try:
            async for evt in service.stream_events(
                explicit_ids=explicit_ids,
                timeout_seconds=1800,
                heartbeat_interval=25.0,
            ):
                yield evt
        finally:
            # Ensure service & session cleanup
            try:
                await service.close()
            except Exception:
                logger.debug("SSE service close failed", exc_info=True)
            # Advance generator to trigger its finally (search_path reset + close)
            try:
                next(sess_gen)
            except StopIteration:
                pass
            except Exception:
                logger.debug("SSE session generator finalization failed", exc_info=True)

    return EventSourceResponse(event_generator())
