"""Deal Timeline API handler - endpoints for accessing deal timeline events."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi import Response as FastAPIResponse
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.deal_timeline_models import (
    TimelineEvent,
    TimelinePage,
)
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables.sync.timeline_models import (
    TimelineEventType,
)
from src.infra.config.timeline_event_metadata import get_timeline_event_source_metadata
from src.infra.utils.logger import get_logger
from src.service.crm.deal_timeline_service import (
    DealTimelineService,
    DealTimelineServiceError,
)

logger = get_logger("deal_timeline_handler")

_UNEXPECTED_ERR_MSG = "An unexpected error occurred while processing your request"

deal_timeline_router = APIRouter()


async def get_deal_timeline_service(
    user: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency that yields a DealTimelineService instance for the current org."""
    service = DealTimelineService(session, user.organisation_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("DealTimelineService close failed", exc_info=True)


@deal_timeline_router.get(
    "/{deal_id}/timeline",
    response_model=GenericResponse[TimelinePage],
    summary="Get deal timeline",
    description="Get paginated timeline events for a deal in chronological order.",
    dependencies=[Depends(rate_limit_user_read)],
)
async def get_deal_timeline(
    deal_id: UUID,
    response: FastAPIResponse,
    service: DealTimelineService = Depends(get_deal_timeline_service),
    limit: int = Query(20, ge=1, le=100, description="Number of events per page"),
    cursor: str | None = Query(None, description="Pagination cursor"),
    event_types: str | None = Query(
        None,
        description="Comma-separated event types to filter (EMAIL,MEETING)",
    ),
    start_date: datetime | None = Query(None, description="Filter events after this date"),
    end_date: datetime | None = Query(None, description="Filter events before this date"),
    include_excluded: bool = Query(False, description="Include excluded/hidden events"),
    sort: str = Query("desc", pattern="^(asc|desc)$", description="Sort order"),
) -> GenericResponse[TimelinePage]:
    """
    Get timeline events for a deal.

    Returns a paginated list of timeline events (emails and meetings)
    sorted chronologically. Use cursor for pagination.

    Query parameters:
    - limit: Number of events per page (1-100, default 20)
    - cursor: Opaque cursor for pagination
    - event_types: Comma-separated filter (EMAIL,MEETING)
    - start_date: Filter events after this date
    - end_date: Filter events before this date
    - include_excluded: Include events that were excluded/rejected
    - sort: Sort order (desc = newest first, asc = oldest first)
    """
    try:
        # Parse event types filter
        parsed_event_types = None
        if event_types:
            type_strs = [t.strip().upper() for t in event_types.split(",")]
            parsed_event_types = []
            for t in type_strs:
                try:
                    parsed_event_types.append(TimelineEventType(t))
                except ValueError:
                    logger.warning(f"Invalid event type filter: {t}")

        events, next_cursor, total_count = await service.get_timeline_events(
            deal_id=deal_id,
            limit=limit,
            cursor=cursor,
            event_types=parsed_event_types,
            start_date=start_date,
            end_date=end_date,
            include_excluded=include_excluded,
            sort_order=sort,
        )

        # Build unified TimelineEvent items with preview event_data
        items: list[TimelineEvent] = []
        for e in events:
            event_source_value = (
                e.event_source.value if hasattr(e.event_source, "value") else e.event_source
            )
            meta = get_timeline_event_source_metadata(event_source_value)
            event_data = {
                **service._build_preview_event_data(e),
                "provider_logo_url": meta.logo_url if meta else None,
            }

            items.append(
                TimelineEvent(
                    id=e.timeline_event_id,
                    deal_id=e.deal_id,
                    event_type=e.event_type.value
                    if hasattr(e.event_type, "value")
                    else e.event_type,
                    event_source=event_source_value,
                    event_timestamp=e.event_timestamp,
                    event_data=event_data,
                    excluded_at=e.excluded_at,
                    created_at=e.created_at,
                    updated_at=e.updated_at,
                )
            )

        return Response.success_response(
            message="Timeline retrieved successfully",
            data=TimelinePage(
                deal_id=deal_id,
                items=items,
                total_count=total_count,
                cursor=next_cursor,
            ),
        )

    except DealTimelineServiceError as e:
        logger.warning(f"Timeline service error: {e.message}")
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception("Failed to get deal timeline")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@deal_timeline_router.get(
    "/timeline/{timeline_event_id}",
    response_model=GenericResponse[TimelineEvent],
    summary="Get timeline event details",
    description="Get full details of a timeline event including enriched event_data.",
    dependencies=[Depends(rate_limit_user_read)],
)
async def get_timeline_event_detail(
    timeline_event_id: UUID,
    response: FastAPIResponse,
    service: DealTimelineService = Depends(get_deal_timeline_service),
) -> GenericResponse[TimelineEvent]:
    """
    Get full details of a specific timeline event.

    Returns the same TimelineEvent structure as the list endpoint,
    but with enriched event_data (e.g., full attendee list for meetings,
    meeting_summary data, to/cc emails for email events).
    """
    try:
        event_dict = await service.get_timeline_event_detail(timeline_event_id)

        if meta := get_timeline_event_source_metadata(event_dict.get("event_source")):
            event_data = dict(event_dict.get("event_data") or {})
            event_data["provider_logo_url"] = meta.logo_url
            event_dict["event_data"] = event_data
        else:
            event_data = dict(event_dict.get("event_data") or {})
            event_data.setdefault("provider_logo_url", None)
            event_dict["event_data"] = event_data

        return Response.success_response(
            message="Timeline event retrieved successfully", data=TimelineEvent(**event_dict)
        )

    except DealTimelineServiceError as e:
        logger.warning(f"Timeline service error: {e.message}")
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception("Failed to get timeline event detail")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )
