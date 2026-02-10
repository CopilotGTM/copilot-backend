"""Deal CSV import API handler.

Provides a three-step workflow for bulk-importing deals into the internal CRM:

1. GET  /import/template           – Download a CSV template with all supported columns.
2. POST /import/upload-url         – Obtain a pre-signed Azure Blob URL to upload the filled CSV.
3. POST /import/process            – Submit the uploaded file for async processing via Celery.
4. GET  /import/jobs/{job_id}/events – SSE stream delivering real-time progress and results.

The template includes first-class CRM columns (deal_name, amount, stage, etc.).
Custom ``custom_*`` columns are accepted for backward compatibility but are not
part of the standard template contract.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import Response as FastAPIResponse
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
from src.api.schemas.crm_deal_import_models import (
    DealImportCustomColumnType,
    DealImportFileType,
    DealImportProcessRequest,
    DealImportProcessResponse,
    DealImportTemplateResponse,
    DealImportUploadRequest,
    DealImportUploadResponse,
)
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.domain.models.organisation_tables.sync.crm.custom_fields import (
    CRMCustomFieldDataType,
    CRMCustomFieldDefinition,
    CRMCustomFieldEntity,
)
from src.infra.utils.logger import get_logger
from src.service.crm.internal.custom_field_service import (
    CustomFieldService,
    CustomFieldServiceError,
)
from src.service.crm.internal.deal_import_service import (
    DealImportService,
    DealImportServiceError,
)
from src.service.crm.jobs.deal_job_sse_service import DealJobSSEService
from src.workers.deal_worker.client import send_deal_task_async
from sse_starlette.sse import EventSourceResponse

logger = get_logger("deal_import_handler")

deal_import_router = APIRouter()


def _map_import_type_to_custom_data_type(
    import_type: DealImportCustomColumnType,
) -> tuple[CRMCustomFieldDataType, dict]:
    if import_type == DealImportCustomColumnType.INTEGER:
        return CRMCustomFieldDataType.INTEGER, {"number_subtype": "integer"}
    if import_type == DealImportCustomColumnType.NUMBER:
        return CRMCustomFieldDataType.NUMBER, {}
    if import_type == DealImportCustomColumnType.CURRENCY:
        return CRMCustomFieldDataType.CURRENCY, {}
    if import_type == DealImportCustomColumnType.DATE:
        return CRMCustomFieldDataType.DATE, {}
    if import_type == DealImportCustomColumnType.DATETIME:
        return CRMCustomFieldDataType.DATETIME, {}
    if import_type == DealImportCustomColumnType.BOOLEAN:
        return CRMCustomFieldDataType.BOOLEAN, {}
    return CRMCustomFieldDataType.TEXT, {}


def _serialize_data_type(data_type: CRMCustomFieldDataType | str) -> str:
    return data_type.value if isinstance(data_type, CRMCustomFieldDataType) else str(data_type)


async def get_deal_import_service(
    user_ids: UserIDs = Depends(get_current_user),
):
    """Dependency that yields a DealImportService instance for the current user.

    Manages the service lifecycle — the underlying blob storage clients are
    closed automatically when the request completes.
    """
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
    """Return a pre-signed download URL for the deal import CSV template.

    The template is generated from server-side header configuration and
    uploaded before each download URL is returned, so clients always receive
    the latest column contract.

    Response includes:
    - download_url: Time-limited SAS URL for direct browser download
    - expires_in_seconds: Remaining lifetime of the download URL
    - storage_path: Internal blob path (used for diagnostics only)
    """
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
    """Generate a time-limited pre-signed URL for uploading a deal import CSV.

    The caller should PUT the filled CSV file to the returned ``upload_url``.
    The blob is stored under a unique path scoped to the current user,
    which is later passed to the /import/process endpoint.

    Response includes:
    - upload_url: Time-limited SAS URL accepting a PUT with the CSV body
    - storage_path: Blob path to supply when triggering processing
    - expires_in_seconds: Remaining lifetime of the upload URL
    """
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
    deal_import_service: DealImportService = Depends(get_deal_import_service),
    _rl: None = Depends(rate_limit_user_write),
) -> GenericResponse[DealImportProcessResponse]:
    """Submit an uploaded CSV for asynchronous deal import processing.

    Validates that:
    - Only CSV file types are accepted
    - The storage_path belongs to the authenticated user (prevents path traversal)

    Creates a DealJobs record with status=PENDING, dispatches a Celery task
    to the deal worker queue, and returns a stream URL for real-time progress.

    Response includes:
    - job_id: Unique identifier for tracking the import job
    - status: Initial job status (PENDING)
    - stream_url: SSE endpoint to subscribe to for progress events
    """
    try:
        if request.file_type and request.file_type != DealImportFileType.CSV:
            return Response.error_response(
                message="Only CSV imports are supported", status_code=400, response=response
            )

        # Prevent path traversal — storage_path must live under the user's own import directory
        expected_prefix = f"{DealImportService.IMPORT_DIR}/{user_ids.user_id}/"
        if not request.storage_path.startswith(expected_prefix):
            return Response.error_response(
                message="Invalid storage path for deal import", status_code=400, response=response
            )

        # Detect custom columns before enqueuing the worker job.
        custom_columns, suggested_types_raw = await deal_import_service.inspect_custom_columns(
            request.storage_path
        )
        resolved_custom_types: dict[str, str] = {}
        if custom_columns:
            custom_field_service = CustomFieldService(
                session=session,
                organisation_id=user_ids.organisation_id,
                actor_user_id=user_ids.user_id,
            )
            existing_field_map = await custom_field_service.get_field_map(
                CRMCustomFieldEntity.DEAL,
                custom_columns,
            )
            unknown_custom_columns = [
                custom_key for custom_key in custom_columns if custom_key not in existing_field_map
            ]
            provided_custom_types = request.custom_column_types or {}

            missing_mappings = [
                custom_key
                for custom_key in unknown_custom_columns
                if custom_key not in provided_custom_types
            ]
            if missing_mappings:
                suggested_types = {
                    custom_key: DealImportCustomColumnType(
                        suggested_types_raw.get(custom_key, DealImportCustomColumnType.TEXT.value)
                    )
                    for custom_key in unknown_custom_columns
                }
                payload = DealImportProcessResponse(
                    status="mapping_required",
                    requires_custom_column_mapping=True,
                    unknown_custom_columns=unknown_custom_columns,
                    suggested_types=suggested_types,
                )
                return Response.error_response(
                    message="Custom column type mapping required before import can continue",
                    status_code=409,
                    response=response,
                    data=payload,
                )

            for custom_key in unknown_custom_columns:
                custom_type = provided_custom_types.get(custom_key, DealImportCustomColumnType.TEXT)
                data_type, validation = _map_import_type_to_custom_data_type(custom_type)
                custom_label = (request.custom_column_labels or {}).get(custom_key)
                try:
                    await custom_field_service.create_field(
                        {
                            "entity": CRMCustomFieldEntity.DEAL.value,
                            "key": custom_key,
                            "label": custom_label
                            or deal_import_service.humanize_custom_key(custom_key),
                            "data_type": data_type.value,
                            "required": False,
                            "is_predefined": False,
                            "options": {},
                            "validation": validation,
                            "formula": None,
                        }
                    )
                except CustomFieldServiceError as exc:
                    # Safe to continue on duplicate key due concurrent imports.
                    if exc.status_code != 409:
                        raise

            field_type_stmt = select(
                col(CRMCustomFieldDefinition.key),
                col(CRMCustomFieldDefinition.data_type),
            ).where(
                col(CRMCustomFieldDefinition.organisation_id) == user_ids.organisation_id,
                col(CRMCustomFieldDefinition.entity_type) == CRMCustomFieldEntity.DEAL.value,
                col(CRMCustomFieldDefinition.is_deleted).is_(False),
                col(CRMCustomFieldDefinition.is_active).is_(True),
                col(CRMCustomFieldDefinition.key).in_(custom_columns),
            )
            field_type_result = await session.execute(field_type_stmt)
            resolved_custom_types = {
                key: _serialize_data_type(data_type) for key, data_type in field_type_result.all()
            }

        # Job record is created first so the frontend can subscribe to SSE before the worker starts
        job = DealJobs(
            organisation_id=user_ids.organisation_id,
            user_id=user_ids.user_id,
            job_type=DealJobType.DEAL_IMPORT,
            deal_id=None,
            status=JobStatus.PENDING,
            job_metadata={
                "storage_path": request.storage_path,
                "file_type": request.file_type.value if request.file_type else None,
                "import_mode": request.import_mode.value,
                "emit_row_events": request.emit_row_events,
                "strict_validation": request.strict_validation,
                "custom_columns": custom_columns or None,
                "custom_column_types": resolved_custom_types or None,
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
    except (DealImportServiceError, CustomFieldServiceError) as exc:
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
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
    request: Request,
    user_ids: UserIDs = Depends(get_current_user),
):
    """Open an SSE connection that streams real-time progress events for a deal import job.

    Events are published by the Celery deal worker as it processes each CSV row.
    The stream includes:
    - progress: Percentage complete with a human-readable stage description
    - result: Final summary with created/failed/warning counts and row-level details
    - error: Emitted if the job fails unexpectedly

    The connection is held open until the job completes or the client disconnects.
    """
    sse_service = DealJobSSEService(user_ids.organisation_id, job_id, user_ids.user_id)
    can_access = await sse_service.ensure_user_access()
    if can_access is False:
        raise HTTPException(status_code=403, detail="Not authorized to access this job stream")

    # SSE spec: clients send Last-Event-ID on reconnect so we can resume without replaying
    last_event_id = request.headers.get("last-event-id") or request.headers.get("Last-Event-ID")
    resume_from_id = last_event_id or None

    logger.info(
        "Client connected to deal import SSE: org=%s job=%s",
        user_ids.organisation_id,
        job_id,
    )

    async def event_generator():
        try:
            async for ev in sse_service.stream_events(resume_from_id=resume_from_id):
                yield ev
        finally:
            try:
                await sse_service.close()
            except Exception:
                logger.debug("DealJobSSE service close failed", exc_info=True)

    return EventSourceResponse(event_generator())
