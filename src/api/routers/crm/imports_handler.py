from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi import Response as FastAPIResponse
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.crm_write_guard import require_crm_write_scope
from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.crm_import_models import (
    CRMImportMappingRequest,
    CRMImportPresignRequest,
    CRMImportPresignResponse,
    CRMImportRowItem,
    CRMImportRowsResponse,
    CRMImportStatusResponse,
)
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables.job_models import DealJobs
from src.domain.models.organisation_tables.sync.crm.imports import CRMImportRow
from src.infra.utils.logger import get_logger
from src.service.crm.imports.import_service import CRMImportService, CRMImportServiceError
from src.workers.deal_worker.client import send_deal_task_async
from starlette.responses import Response as StarletteResponse

logger = get_logger("crm_imports_handler")

imports_router = APIRouter()
_UNEXPECTED_ERR_MSG = "An unexpected error occurred while processing your request"


async def get_crm_import_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
    master_session: AsyncSession = Depends(get_master_async_session),
):
    async def send_process_task(*, organisation_id: UUID, import_job_id: UUID) -> None:
        await send_deal_task_async(
            "crm_import.process_csv",
            kwargs={
                "organisation_id": str(organisation_id),
                "import_job_id": str(import_job_id),
            },
            queue="deal",
        )

    return CRMImportService(
        org_session,
        user_ids.organisation_id,
        user_ids.user_id,
        task_sender=send_process_task,
        master_session=master_session,
    )


@imports_router.post(
    "/imports/csv/presign",
    response_model=GenericResponse[CRMImportPresignResponse],
    tags=["crm-imports"],
)
async def presign_csv_import(
    body: CRMImportPresignRequest,
    response: FastAPIResponse,
    service: CRMImportService = Depends(get_crm_import_service),
    _rl_write: None = Depends(rate_limit_user_write),
    _accounts_create_guard: None = Depends(require_crm_write_scope("accounts:create")),
    _contacts_create_guard: None = Depends(require_crm_write_scope("contacts:create")),
    _deals_create_guard: None = Depends(require_crm_write_scope("deals:create")),
) -> GenericResponse[CRMImportPresignResponse]:
    try:
        result = await service.presign(
            body.filename,
            mode=body.mode,
            options={"update_existing": body.update_existing},
        )
        data = CRMImportPresignResponse(
            import_id=result.import_id,
            upload_url=result.upload_url,
            storage_path=result.storage_path,
        )
        return Response.created_response(
            "CRM import upload URL created", data=data, response=response
        )
    except CRMImportServiceError as exc:
        return Response.error_response(
            exc.status_code, exc.message, data=exc.data, response=response
        )
    except Exception:
        logger.exception("Unexpected error creating CRM CSV import upload URL")
        return Response.internal_server_error_response(_UNEXPECTED_ERR_MSG, response=response)


@imports_router.post(
    "/imports/csv/{import_id}/finalize",
    response_model=GenericResponse[CRMImportStatusResponse],
    tags=["crm-imports"],
)
async def finalize_csv_import(
    import_id: UUID,
    response: FastAPIResponse,
    service: CRMImportService = Depends(get_crm_import_service),
    _rl_write: None = Depends(rate_limit_user_write),
    _accounts_create_guard: None = Depends(require_crm_write_scope("accounts:create")),
    _contacts_create_guard: None = Depends(require_crm_write_scope("contacts:create")),
    _deals_create_guard: None = Depends(require_crm_write_scope("deals:create")),
) -> GenericResponse[CRMImportStatusResponse]:
    try:
        result = await service.finalize(import_id)
        await send_deal_task_async(
            "crm_import.analyze_csv",
            kwargs={
                "organisation_id": str(result.job.organisation_id),
                "import_job_id": str(import_id),
            },
            queue="deal",
        )
        return Response.accepted_response(
            "CRM import finalized for analysis",
            data=_present_status(result.job),
            response=response,
        )
    except CRMImportServiceError as exc:
        return Response.error_response(
            exc.status_code, exc.message, data=exc.data, response=response
        )
    except Exception:
        logger.exception("Unexpected error finalizing CRM CSV import %s", import_id)
        return Response.internal_server_error_response(_UNEXPECTED_ERR_MSG, response=response)


@imports_router.get(
    "/imports/csv/{import_id}",
    response_model=GenericResponse[CRMImportStatusResponse],
    tags=["crm-imports"],
)
async def get_csv_import_status(
    import_id: UUID,
    response: FastAPIResponse,
    service: CRMImportService = Depends(get_crm_import_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[CRMImportStatusResponse]:
    try:
        result = await service.get_status(import_id)
        latest_errors = await service.list_rows(import_id, limit=5, status="failed")
        return Response.success_response(
            "CRM import status retrieved",
            data=_present_status(
                result.job,
                latest_errors=[_present_error(row) for row in latest_errors.items],
            ),
        )
    except CRMImportServiceError as exc:
        return Response.error_response(
            exc.status_code, exc.message, data=exc.data, response=response
        )
    except Exception:
        logger.exception("Unexpected error retrieving CRM CSV import %s", import_id)
        return Response.internal_server_error_response(_UNEXPECTED_ERR_MSG, response=response)


@imports_router.patch(
    "/imports/csv/{import_id}/mapping",
    response_model=GenericResponse[CRMImportStatusResponse],
    tags=["crm-imports"],
)
async def save_csv_import_mapping(
    import_id: UUID,
    body: CRMImportMappingRequest,
    response: FastAPIResponse,
    service: CRMImportService = Depends(get_crm_import_service),
    _rl_write: None = Depends(rate_limit_user_write),
    _accounts_create_guard: None = Depends(require_crm_write_scope("accounts:create")),
    _contacts_create_guard: None = Depends(require_crm_write_scope("contacts:create")),
    _deals_create_guard: None = Depends(require_crm_write_scope("deals:create")),
) -> GenericResponse[CRMImportStatusResponse]:
    try:
        result = await service.save_mapping(
            import_id,
            body.mapping,
            options={"update_existing": body.update_existing},
        )
        return Response.success_response(
            "CRM import mapping saved", data=_present_status(result.job)
        )
    except CRMImportServiceError as exc:
        return Response.error_response(
            exc.status_code, exc.message, data=exc.data, response=response
        )
    except Exception:
        logger.exception("Unexpected error saving CRM CSV import mapping %s", import_id)
        return Response.internal_server_error_response(_UNEXPECTED_ERR_MSG, response=response)


@imports_router.post(
    "/imports/csv/{import_id}/start",
    response_model=GenericResponse[CRMImportStatusResponse],
    tags=["crm-imports"],
)
async def start_csv_import(
    import_id: UUID,
    response: FastAPIResponse,
    service: CRMImportService = Depends(get_crm_import_service),
    _rl_write: None = Depends(rate_limit_user_write),
    _accounts_create_guard: None = Depends(require_crm_write_scope("accounts:create")),
    _contacts_create_guard: None = Depends(require_crm_write_scope("contacts:create")),
    _deals_create_guard: None = Depends(require_crm_write_scope("deals:create")),
) -> GenericResponse[CRMImportStatusResponse]:
    try:
        result = await service.start(import_id)
        return Response.accepted_response(
            "CRM import started",
            data=_present_status(result.job),
            response=response,
        )
    except CRMImportServiceError as exc:
        return Response.error_response(
            exc.status_code, exc.message, data=exc.data, response=response
        )
    except Exception:
        logger.exception("Unexpected error starting CRM CSV import %s", import_id)
        return Response.internal_server_error_response(_UNEXPECTED_ERR_MSG, response=response)


@imports_router.get(
    "/imports/csv/{import_id}/rows",
    response_model=GenericResponse[CRMImportRowsResponse],
    tags=["crm-imports"],
)
async def list_csv_import_rows(
    import_id: UUID,
    response: FastAPIResponse,
    cursor: int | None = Query(None, ge=0),
    limit: int = Query(100, ge=1, le=500),
    status: str | None = Query(None),
    service: CRMImportService = Depends(get_crm_import_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[CRMImportRowsResponse]:
    try:
        result = await service.list_rows(import_id, cursor=cursor, limit=limit, status=status)
        data = CRMImportRowsResponse(
            items=[_present_row(row) for row in result.items],
            next_cursor=result.next_cursor,
            has_more=result.has_more,
        )
        return Response.success_response("CRM import rows retrieved", data=data)
    except CRMImportServiceError as exc:
        return Response.error_response(
            exc.status_code, exc.message, data=exc.data, response=response
        )
    except Exception:
        logger.exception("Unexpected error listing CRM CSV import rows %s", import_id)
        return Response.internal_server_error_response(_UNEXPECTED_ERR_MSG, response=response)


@imports_router.get(
    "/imports/csv/{import_id}/errors.csv",
    tags=["crm-imports"],
    response_model=None,
)
async def download_csv_import_errors(
    import_id: UUID,
    response: FastAPIResponse,
    service: CRMImportService = Depends(get_crm_import_service),
    _rl_read: None = Depends(rate_limit_user_read),
):
    try:
        csv_text = await service.build_error_csv(import_id)
        return StarletteResponse(
            content=csv_text,
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="crm-import-{import_id}-errors.csv"'
            },
        )
    except CRMImportServiceError as exc:
        return Response.error_response(
            exc.status_code, exc.message, data=exc.data, response=response
        )
    except Exception:
        logger.exception("Unexpected error building CRM CSV import errors %s", import_id)
        return Response.internal_server_error_response(_UNEXPECTED_ERR_MSG, response=response)


def _present_status(
    job: DealJobs,
    *,
    latest_errors: list[dict[str, Any]] | None = None,
) -> CRMImportStatusResponse:
    metadata = job.job_metadata or {}
    counts = _counts(job, metadata)
    total_rows = counts.get("total_rows") or 0
    processed_rows = counts.get("processed_rows") or 0
    percent_complete = round((processed_rows / total_rows) * 100, 2) if total_rows else 0.0
    return CRMImportStatusResponse(
        import_id=job.job_id,
        status=_enum_value(job.status),
        crm_import_status=metadata.get("crm_import_status"),
        filename=metadata.get("filename"),
        storage_path=metadata.get("storage_path"),
        analysis=metadata.get("analysis"),
        mapping=metadata.get("mapping") or {},
        mapping_suggestions=metadata.get("mapping_suggestions") or [],
        counts=counts,
        percent_complete=percent_complete,
        latest_errors=latest_errors
        if latest_errors is not None
        else metadata.get("latest_errors") or [],
        mapping_confidence_summary=_summarize_mapping_confidence(
            metadata.get("mapping_suggestions") or []
        ),
    )


def _counts(job: DealJobs, metadata: dict[str, Any]) -> dict[str, int]:
    counts = dict(job.result_data or {})
    analysis = metadata.get("analysis") or {}
    if "total_rows" not in counts and analysis.get("total_rows") is not None:
        counts["total_rows"] = int(analysis["total_rows"])
    counts.setdefault("processed_rows", 0)
    counts.setdefault("created_accounts", 0)
    counts.setdefault("created_contacts", 0)
    counts.setdefault("created_deals", 0)
    counts.setdefault("skipped_duplicates", 0)
    counts.setdefault("failed_rows", 0)
    return counts


def _summarize_mapping_confidence(suggestions: list[dict[str, Any]]) -> dict[str, int]:
    summary = {"high": 0, "medium": 0, "low": 0}
    for suggestion in suggestions:
        confidence = suggestion.get("confidence")
        if confidence in summary:
            summary[confidence] += 1
            continue
        if isinstance(confidence, str):
            summary["low"] += 1
            continue
        numeric_confidence = float(confidence or 0)
        if numeric_confidence >= 0.8:
            summary["high"] += 1
        elif numeric_confidence >= 0.5:
            summary["medium"] += 1
        else:
            summary["low"] += 1
    return summary


def _present_row(row: CRMImportRow) -> CRMImportRowItem:
    return CRMImportRowItem(
        row_number=row.row_number,
        status=_enum_value(row.status),
        target_type=row.target_type,
        account_id=row.account_id,
        contact_id=row.contact_id,
        deal_id=row.deal_id,
        error_code=row.error_code,
        error_message=row.error_message,
        raw_values=row.raw_values or {},
        normalized_values=row.normalized_values or {},
    )


def _present_error(row: CRMImportRow) -> dict[str, Any]:
    return {
        "row_number": row.row_number,
        "status": _enum_value(row.status),
        "error_code": row.error_code,
        "error_message": row.error_message,
    }


def _enum_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)
