from __future__ import annotations

import asyncio
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.knowledge_access_models import (
    KnowledgeDeleteResponse,
    KnowledgeDownloadResponse,
    KnowledgeSourceListItem,
    KnowledgeSourcesPage,
    KnowledgeSourcesStats,
)
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables.knowledge_models import (
    OrganisationSource,
    OrganisationSourceStatus,
    SourceType,
)
from src.infra.config.constant import KnowledgeBaseErrors
from src.infra.utils.logger import get_logger
from src.service.knowledge_base.knowledge_access_service import (
    KnowledgeAccessService,
    KnowledgeAccessServiceError,
)
from src.workers.knowledge_worker.celery_app import app as knowledge_app
from src.api.dependencies.rate_limit import rate_limit_user_read

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
    service = KnowledgeAccessService(
        org_session, user_ids.organisation_id, user_ids.user_id
    )
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
    cursor: Optional[str] = Query(
        None, description="Opaque cursor returned by previous page (omit for first)"
    ),
    statuses: Optional[List[OrganisationSourceStatus]] = Query(
        None, description="Optional list of statuses to filter"
    ),
    source_types: Optional[List[SourceType]] = Query(
        None, description="Optional list of source types (e.g. file, integration)"
    ),
    q: Optional[str] = Query(
        None, description="Case-insensitive filename search fragment"
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
        )
        # Build item dicts with optional URLs concurrently
        items: List[KnowledgeSourceListItem] = []
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
                items.append(
                    KnowledgeSourceListItem(
                        source_id=src.source_id,
                        filename=src.filename,
                        mimetype=src.mimetype,
                        size_bytes=src.size_bytes,
                        status=_get_status_str(src.status),
                        created_at=src.created_at,
                        updated_at=src.updated_at,
                        error_message=src.error_message,
                        checksum=getattr(src, "checksum", None),
                    )
                )
        page = KnowledgeSourcesPage(items=items, cursor=next_cursor)
        return Response.success_response("Sources listed", page)
    except KnowledgeAccessServiceError as e:
        return Response.error_response(status_code=e.status_code, message=e.message)
    except Exception as e:  # pragma: no cover - broad safety
        logger.error("Unexpected error listing sources: %s", e, exc_info=True)
        return Response.internal_server_error_response(
            KnowledgeBaseErrors.UNEXPECTED_ERROR
        )


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
        return Response.internal_server_error_response(
            KnowledgeBaseErrors.UNEXPECTED_ERROR
        )


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
        return Response.success_response(
            "Source stats retrieved", KnowledgeSourcesStats(**stats)
        )
    except KnowledgeAccessServiceError as e:
        return Response.error_response(status_code=e.status_code, message=e.message)
    except Exception as e:  # pragma: no cover
        logger.error("Unexpected error getting source stats: %s", e, exc_info=True)
        return Response.internal_server_error_response(
            KnowledgeBaseErrors.UNEXPECTED_ERROR
        )


@access_router.delete(
    "/source/{source_id}",
    response_model=GenericResponse[KnowledgeDeleteResponse],
    tags=["knowledge_access"],
    summary="Request deletion of a knowledge source and its derived records",
    status_code=status.HTTP_202_ACCEPTED,
)
async def delete_source(
    source_id: UUID,
    service: KnowledgeAccessService = Depends(get_knowledge_access_service),
):
    """Enqueue deletion of a source and all related records.

    Behaviour:
    - Validates access to the source (org + ownership).
    - Marks the source as soft-deleted (`is_deleted` = True).
    - Enqueues background task to delete DB records and blobs.
    - Returns 202 Accepted on successful enqueue.
    """
    try:
        # Permission check: source must exist and belong to caller
        src = await service.session.get(OrganisationSource, source_id)
        if (
            not src
            or src.organisation_id != service.organisation_id
            or src.user_id != service.user_id
        ):
            return Response.not_found_error_response(
                "Source not found or access denied"
            )

        # Idempotent check: if already soft-deleted, return an accepted response
        if getattr(src, "is_deleted", False):
            return Response.accepted_response(
                "Deletion already in progress or completed",
                KnowledgeDeleteResponse(source_id=source_id, status="deleted"),
            )

        # Mark as soft-deleted immediately
        if hasattr(src, "is_deleted"):
            src.is_deleted = True
            try:
                service.session.add(src)
            except Exception:
                await service.session.rollback()
                logger.error(
                    "Soft delete commit failed for %s", source_id, exc_info=True
                )
                return Response.internal_server_error_response(
                    "Failed to update source status"
                )

        # Enqueue deletion task in knowledge worker (fast lane queue)
        try:
            knowledge_app.send_task(
                "knowledge.delete_source",
                args=[str(service.organisation_id), str(source_id)],
                queue="knowledge_upload",
                routing_key="knowledge_upload",
            )
        except Exception as task_err:
            logger.error(
                "Failed to enqueue deletion for source %s: %s", source_id, task_err
            )
            return Response.internal_server_error_response(
                "Failed to enqueue delete request"
            )

        delete_resp = KnowledgeDeleteResponse(
            source_id=source_id,
            status="deleting",
        )
        return Response.accepted_response("Deletion enqueued", delete_resp)
    except Exception as e:  # pragma: no cover
        logger.error("Unexpected error enqueuing deletion: %s", e, exc_info=True)
        return Response.internal_server_error_response(
            KnowledgeBaseErrors.UNEXPECTED_ERROR
        )
