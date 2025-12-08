from __future__ import annotations

import asyncio
from datetime import datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Body, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.knowledge_access_models import (
    ACTIVE_STATUSES,
    KnowledgeBatchDeleteRequest,
    KnowledgeBatchDeleteResponse,
    KnowledgeDeleteResultItem,
    KnowledgeDownloadResponse,
    KnowledgeSourceListItem,
    KnowledgeSourcesPage,
    KnowledgeSourcesStats,
)
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables.knowledge_models import (
    OrganisationSourceStatus,
    SourceType,
)
from src.infra.config.constant import KnowledgeBaseErrors
from src.infra.utils.logger import get_logger
from src.service.knowledge_base.knowledge_access_service import (
    KnowledgeAccessService,
    KnowledgeAccessServiceError,
)
from src.workers.knowledge_worker.client import send_knowledge_task_async

logger = get_logger("knowledge_access_handler")

# Common error message reused across handlers (Sonar rule S1192)
_UNEXPECTED_ERR_MSG = "An unexpected error occurred"

access_router = APIRouter()


def _get_status_str(status_enum):
    """Safely get the string value of a status enum."""
    if status_enum is None:
        return None
    return getattr(status_enum, "value", str(status_enum))


async def get_knowledge_access_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
):
    """
    Dependency yielding a KnowledgeAccessService instance for the current org/user.
    Cleanly disposes the service on exit, sync/async safe.
    """
    service = KnowledgeAccessService(org_session, user_ids.organisation_id, user_ids.user_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("Access service close failed", exc_info=True)


@access_router.get(
    "/sources",
    response_model=GenericResponse[KnowledgeSourcesPage],
    tags=["knowledge_access"],
    summary="List knowledge sources (paginated)",
)
async def list_sources(
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = Query(
        None, description="Opaque cursor returned by previous page (omit for first)"
    ),
    statuses: list[OrganisationSourceStatus] | None = Query(
        None,
        description="Optional list of statuses to filter. Note: 'failed' status is excluded by default unless explicitly requested.",
    ),
    source_types: list[SourceType] | None = Query(
        None, description="Optional list of source types (e.g. file, integration)"
    ),
    q: str | None = Query(None, description="Case-insensitive filename search fragment"),
    updated_after: datetime | None = Query(
        None,
        description="Filter sources updated after this timestamp (ISO8601). Useful for fetching recent failures.",
    ),
    include_urls: bool = Query(
        False,
        description="If true include read + thumbnail SAS URLs (extra storage calls).",
    ),
    service: KnowledgeAccessService = Depends(get_knowledge_access_service),
    _rl_read: None = Depends(rate_limit_user_read),
):
    try:
        sources, next_cursor = await service.list_sources(
            limit=limit,
            cursor=cursor,
            statuses=statuses,
            source_types=source_types,
            q=q,
            updated_after=updated_after,
        )
        # Build item dicts with optional URLs concurrently
        items: list[KnowledgeSourceListItem] = []
        if include_urls:
            # Generate SAS URLs concurrently for better latency across the page
            tasks = [service.build_item_dict(src, include_urls=True) for src in sources]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for res in results:
                if isinstance(res, Exception):
                    logger.debug("build_item_dict failed during listing", exc_info=True)
                    continue
                items.append(KnowledgeSourceListItem(**res))
        else:
            for src in sources:
                status_str = _get_status_str(src.status)
                items.append(
                    KnowledgeSourceListItem(
                        source_id=src.source_id,
                        filename=src.filename,
                        mimetype=src.mimetype,
                        size_bytes=src.size_bytes,
                        status=status_str,
                        created_at=src.created_at,
                        updated_at=src.updated_at,
                        error_message=src.error_message,
                        checksum=getattr(src, "checksum", None),
                        is_active=status_str in ACTIVE_STATUSES,
                    )
                )
        page = KnowledgeSourcesPage(items=items, cursor=next_cursor)
        return Response.success_response("Sources listed", page)
    except KnowledgeAccessServiceError as e:
        return Response.error_response(status_code=e.status_code, message=e.message)
    except Exception as e:  # pragma: no cover - broad safety
        logger.error("Unexpected error listing sources: %s", e, exc_info=True)
        return Response.internal_server_error_response(KnowledgeBaseErrors.UNEXPECTED_ERROR)


@access_router.get(
    "/source/{source_id}/download",
    response_model=GenericResponse[KnowledgeDownloadResponse],
    tags=["knowledge_access"],
    summary="Generate a download URL for a source",
)
async def download_source(
    source_id: UUID,
    expires_minutes: int = Query(120, ge=1, le=720),
    service: KnowledgeAccessService = Depends(get_knowledge_access_service),
    _rl_read: None = Depends(rate_limit_user_read),
):
    try:
        url = await service.generate_download_url(source_id, minutes=expires_minutes)
        resp = KnowledgeDownloadResponse(
            source_id=source_id,
            download_url=url,
            expires_in_seconds=expires_minutes * 60,
        )
        return Response.success_response("Download URL generated", resp)
    except KnowledgeAccessServiceError as e:
        return Response.error_response(status_code=e.status_code, message=e.message)
    except Exception as e:  # pragma: no cover
        logger.error("Unexpected error generating download URL: %s", e, exc_info=True)
        return Response.internal_server_error_response(KnowledgeBaseErrors.UNEXPECTED_ERROR)


@access_router.get(
    "/sources/stats",
    response_model=GenericResponse[KnowledgeSourcesStats],
    tags=["knowledge_access"],
    summary="Aggregated counts for sources (total, by status, by type)",
)
async def sources_stats(
    service: KnowledgeAccessService = Depends(get_knowledge_access_service),
    _rl_read: None = Depends(rate_limit_user_read),
):
    try:
        stats = await service.get_stats()
        return Response.success_response("Source stats retrieved", KnowledgeSourcesStats(**stats))
    except KnowledgeAccessServiceError as e:
        return Response.error_response(status_code=e.status_code, message=e.message)
    except Exception as e:  # pragma: no cover
        logger.error("Unexpected error getting source stats: %s", e, exc_info=True)
        return Response.internal_server_error_response(KnowledgeBaseErrors.UNEXPECTED_ERROR)


@access_router.delete(
    "/sources",
    response_model=GenericResponse[KnowledgeBatchDeleteResponse],
    tags=["knowledge_access"],
    summary="Delete one or more knowledge sources",
    status_code=status.HTTP_202_ACCEPTED,
)
async def delete_sources(
    payload: KnowledgeBatchDeleteRequest = Body(...),
    service: KnowledgeAccessService = Depends(get_knowledge_access_service),
    _rl: None = Depends(rate_limit_user_write),
):
    """Enqueue deletion of one or more sources and their related records.

    Supports both single and batch deletion - pass one or more source_ids.

    Behaviour:
    - Validates access to each source (org + ownership).
    - Creates deletion jobs for tracking.
    - Marks sources as soft-deleted (`is_deleted` = True) and status as DELETING.
    - Enqueues background tasks for each source.
    - Returns 202 Accepted with per-source results and any errors.
    - Partial success supported: some sources may succeed while others fail.
    """
    try:
        batch_id = uuid4()
        successes, errors = await service.request_batch_deletion(payload.source_ids)

        results: list[KnowledgeDeleteResultItem] = []

        # Enqueue tasks for successful deletions
        for src, job in successes:
            try:
                await send_knowledge_task_async(
                    "knowledge.delete_source",
                    args=[str(service.organisation_id), str(src.source_id), str(job.job_id)],
                    queue="knowledge_upload",
                    routing_key="knowledge_upload",
                )
                results.append(
                    KnowledgeDeleteResultItem(
                        source_id=src.source_id,
                        status="deleting",
                        job_id=job.job_id,
                    )
                )
            except Exception as task_err:
                logger.error(
                    "Failed to enqueue deletion for source %s: %s", src.source_id, task_err
                )
                errors.append(
                    {
                        "source_id": str(src.source_id),
                        "reason": "enqueue_failed",
                        "message": f"Failed to enqueue task: {task_err}",
                    }
                )

        # Convert error dicts to result items for already_deleted cases
        error_results = []
        for err in errors:
            if err.get("reason") == "already_deleted":
                results.append(
                    KnowledgeDeleteResultItem(
                        source_id=UUID(err["source_id"]),
                        status="already_deleted",
                        job_id=None,
                    )
                )
            else:
                error_results.append(err)

        response = KnowledgeBatchDeleteResponse(
            batch_id=batch_id,
            results=results,
            errors=error_results,
        )

        if error_results and not results:
            return Response.error_response(
                status_code=status.HTTP_400_BAD_REQUEST,
                message="All deletion requests failed",
                data=response,
            )

        message = "Deletion enqueued"
        if len(payload.source_ids) > 1:
            message = f"Batch deletion enqueued ({len(results)} sources)"
        if error_results:
            message = f"{message} with {len(error_results)} errors"

        return Response.accepted_response(message, response)
    except Exception as e:  # pragma: no cover
        logger.error("Unexpected error in deletion: %s", e, exc_info=True)
        return Response.internal_server_error_response(KnowledgeBaseErrors.UNEXPECTED_ERROR)
