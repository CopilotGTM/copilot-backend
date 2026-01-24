from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi import Response as FastAPIResponse
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.crm_deal_import_models import (
    DealImportFileType,
    DealImportProcessRequest,
    DealImportProcessResponse,
    DealImportTemplateResponse,
    DealImportUploadRequest,
    DealImportUploadResponse,
)
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.infra.utils.logger import get_logger
from src.service.crm.internal.deal_import_service import (
    DealImportService,
    DealImportServiceError,
)
from src.service.crm.jobs.deal_job_sse_service import DealJobSSEService
from src.workers.deal_worker.client import send_deal_task_async
from sse_starlette.sse import EventSourceResponse

logger = get_logger("deal_import_handler")

deal_import_router = APIRouter()


async def get_deal_import_service(
    user_ids: UserIDs = Depends(get_current_user),
):
    service = DealImportService(user_ids.organisation_id, user_ids.user_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("DealImportService close failed", exc_info=True)


@deal_import_router.get(
    "/import/template",
    response_model=GenericResponse[DealImportTemplateResponse],
    tags=["deals"],
    summary="Download deal import template",
)
async def get_deal_import_template(
    response: FastAPIResponse,
    file_type: DealImportFileType = Query(DealImportFileType.CSV),
    service: DealImportService = Depends(get_deal_import_service),
):
    try:
        download_url, blob_path, expires = await service.get_template_download(file_type.value)
        payload = DealImportTemplateResponse(
            file_type=file_type,
            download_url=download_url,
            expires_in_seconds=expires,
            storage_path=blob_path,
        )
        return Response.success_response(message="Template ready", data=payload)
    except DealImportServiceError as exc:
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        logger.error("Unexpected error generating template: %s", exc, exc_info=True)
        return Response.error_response(
            message="Unexpected error generating template", status_code=500, response=response
        )


@deal_import_router.post(
    "/import/upload-url",
    response_model=GenericResponse[DealImportUploadResponse],
    tags=["deals"],
    summary="Create upload URL for deal import file",
)
async def create_deal_import_upload_url(
    request: DealImportUploadRequest,
    response: FastAPIResponse,
    service: DealImportService = Depends(get_deal_import_service),
    _rl: None = Depends(rate_limit_user_write),
):
    try:
        upload_url, blob_path, expires = await service.get_upload_url(request.filename)
        payload = DealImportUploadResponse(
            upload_url=upload_url,
            storage_path=blob_path,
            expires_in_seconds=expires,
        )
        return Response.success_response(message="Upload URL created", data=payload)
    except DealImportServiceError as exc:
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        logger.error("Unexpected error creating upload url: %s", exc, exc_info=True)
        return Response.error_response(
            message="Unexpected error creating upload url", status_code=500, response=response
        )


@deal_import_router.post(
    "/import/process",
    response_model=GenericResponse[DealImportProcessResponse],
    status_code=202,
    tags=["deals"],
    summary="Process an uploaded deal import file",
)
async def process_deal_import(
    request: DealImportProcessRequest,
    response: FastAPIResponse,
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
    _rl: None = Depends(rate_limit_user_write),
) -> GenericResponse[DealImportProcessResponse]:
    """Queue a background job to process a deal import CSV."""
    try:
        if request.file_type and request.file_type != DealImportFileType.CSV:
            return Response.error_response(
                message="Only CSV imports are supported", status_code=400, response=response
            )
        expected_prefix = f"{DealImportService.IMPORT_DIR}/{user_ids.user_id}/"
        if not request.storage_path.startswith(expected_prefix):
            return Response.error_response(
                message="Invalid storage path for deal import", status_code=400, response=response
            )

        job = DealJobs(
            organisation_id=user_ids.organisation_id,
            user_id=user_ids.user_id,
            job_type=DealJobType.DEAL_IMPORT,
            deal_id=None,
            status=JobStatus.PENDING,
            job_metadata={
                "storage_path": request.storage_path,
                "file_type": request.file_type.value if request.file_type else None,
            },
        )
        session.add(job)
        await session.commit()
        await session.refresh(job)

        await send_deal_task_async(
            "deal_import_task.process",
            args=[str(user_ids.organisation_id), str(job.job_id)],
            queue="deal",
        )

        stream_url = f"/users/crm/deals/import/jobs/{job.job_id}/events"
        payload = DealImportProcessResponse(
            job_id=job.job_id,
            job_type=str(job.job_type),
            status=str(job.status),
            stream_url=stream_url,
        )
        return Response.accepted_response(
            message="Deal import processing started", data=payload, response=response
        )
    except Exception as exc:
        logger.error("Unexpected error starting deal import: %s", exc, exc_info=True)
        return Response.error_response(
            message="Unexpected error starting deal import", status_code=500, response=response
        )


@deal_import_router.get(
    "/import/jobs/{job_id}/events",
    tags=["deals"],
    summary="SSE stream for deal import job",
)
async def deal_import_stream(
    job_id: UUID,
    user_ids: UserIDs = Depends(get_current_user),
):
    sse_service = DealJobSSEService(user_ids.organisation_id, job_id, user_ids.user_id)
    logger.info(
        "Client connected to deal import SSE: org=%s job=%s",
        user_ids.organisation_id,
        job_id,
    )

    async def event_generator():
        try:
            async for ev in sse_service.stream_events():
                yield ev
        finally:
            try:
                await sse_service.close()
            except Exception:
                logger.debug("DealJobSSE service close failed", exc_info=True)

    return EventSourceResponse(event_generator())
