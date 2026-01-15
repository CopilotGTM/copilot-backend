"""Risks & Blockers API handler for managing deal risks and blockers."""

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
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.risk_blocker_models import (
    RiskBlockerCreateRequest,
    RiskBlockerDetail,
    RiskBlockerListResponse,
    RiskBlockerResolveRequest,
    RiskBlockerUpdateRequest,
)
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables.sync.risk_blocker_models import (
    RiskBlockerCategory,
    RiskBlockerSeverity,
    RiskBlockerStatus,
    RiskBlockerType,
)
from src.infra.utils.logger import get_logger
from src.service.crm.risk_blocker_service import (
    InvalidStatusTransitionError,
    RiskBlockerNotFoundError,
    RiskBlockerService,
    RiskBlockerServiceError,
)

logger = get_logger("risks_blockers_handler")

_UNEXPECTED_ERR_MSG = "An unexpected error occurred while processing your request"

risks_blockers_router = APIRouter()


async def get_risk_blocker_service(
    user: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency that yields a RiskBlockerService instance for the current user/org."""
    service = RiskBlockerService(
        session=session,
        organisation_id=user.organisation_id,
        user_id=user.user_id,
        user_name=user.user_name if hasattr(user, "user_name") else None,
    )
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("RiskBlockerService close failed", exc_info=True)


@risks_blockers_router.get(
    "/{deal_id}/risks-blockers",
    response_model=GenericResponse[RiskBlockerListResponse],
    summary="List risks and blockers for a deal",
    description="Get paginated list of risks and blockers for a deal with filtering options.",
    dependencies=[Depends(rate_limit_user_read)],
)
async def list_risks_blockers(
    deal_id: UUID,
    response: FastAPIResponse,
    service: RiskBlockerService = Depends(get_risk_blocker_service),
    limit: int = Query(20, ge=1, le=100, description="Number of items per page"),
    cursor: str | None = Query(None, description="Pagination cursor"),
    item_type: RiskBlockerType | None = Query(None, description="Filter by RISK or BLOCKER"),
    status: str | None = Query(
        None,
        description="Comma-separated statuses to filter (OPEN,IN_PROGRESS,RESOLVED)",
    ),
    severity: RiskBlockerSeverity | None = Query(None, description="Filter by severity"),
    category: RiskBlockerCategory | None = Query(None, description="Filter by category"),
    include_resolved: bool = Query(False, description="Include resolved/dismissed items"),
    sort: str = Query("desc", pattern="^(asc|desc)$", description="Sort order"),
) -> GenericResponse[RiskBlockerListResponse]:
    """
    Get risks and blockers for a deal.

    Returns a paginated list sorted by creation date. AI-suggested items
    that haven't been confirmed appear with status=AI_SUGGESTED.

    By default, resolved and dismissed items are excluded. Use include_resolved=true
    to see all items including closed ones.

    Query parameters:
    - limit: Number of items per page (1-100, default 20)
    - cursor: Opaque cursor for pagination
    - item_type: Filter by RISK or BLOCKER
    - status: Comma-separated status filter (OPEN,IN_PROGRESS,AI_SUGGESTED,etc.)
    - severity: Filter by severity level (CRITICAL,HIGH,MEDIUM,LOW)
    - category: Filter by category (TECHNICAL,BUDGET,COMPETITION,etc.)
    - include_resolved: Include resolved/dismissed items (default false)
    - sort: Sort order (desc = newest first, asc = oldest first)
    """
    try:
        # Parse status filter
        parsed_statuses = None
        if status:
            status_strs = [s.strip().upper() for s in status.split(",")]
            parsed_statuses = []
            for s in status_strs:
                try:
                    parsed_statuses.append(RiskBlockerStatus(s))
                except ValueError:
                    logger.warning(f"Invalid status filter: {s}")

        items, next_cursor, total_count, summary = await service.list_for_deal(
            deal_id=deal_id,
            limit=limit,
            cursor=cursor,
            item_type=item_type,
            statuses=parsed_statuses,
            severity=severity,
            category=category,
            include_resolved=include_resolved,
            sort_order=sort,
        )

        return Response.success_response(
            message="Risks and blockers retrieved successfully",
            data=RiskBlockerListResponse(
                deal_id=deal_id,
                items=items,
                total_count=total_count,
                cursor=next_cursor,
                summary=summary,
            ),
        )

    except RiskBlockerServiceError as e:
        logger.warning(f"Risk/blocker service error: {e.message}")
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception("Failed to list risks/blockers")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@risks_blockers_router.get(
    "/risks-blockers/{risk_blocker_id}",
    response_model=GenericResponse[RiskBlockerDetail],
    summary="Get risk/blocker details",
    description="Get full details of a specific risk or blocker including audit trail.",
    dependencies=[Depends(rate_limit_user_read)],
)
async def get_risk_blocker(
    risk_blocker_id: UUID,
    response: FastAPIResponse,
    service: RiskBlockerService = Depends(get_risk_blocker_service),
) -> GenericResponse[RiskBlockerDetail]:
    """
    Get full details of a risk/blocker.

    Returns the complete record including:
    - Full content (title, description, quote)
    - AI metadata (confidence, reasoning, extraction run ID)
    - Source attribution (what meeting/email/message it came from)
    - Assignment info (owner, team)
    - Resolution details if resolved
    - Complete audit trail of all changes
    """
    try:
        item = await service.get_by_id(risk_blocker_id)

        if not item:
            return Response.error_response(
                message="Risk/blocker not found", status_code=404, response=response
            )

        return Response.success_response(
            message="Risk/blocker retrieved successfully",
            data=item,
        )

    except RiskBlockerServiceError as e:
        logger.warning(f"Risk/blocker service error: {e.message}")
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception("Failed to get risk/blocker")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


# ─────────────────────────────────────────────────────────────────────────────
# Create Endpoint
# ─────────────────────────────────────────────────────────────────────────────


@risks_blockers_router.post(
    "/{deal_id}/risks-blockers",
    response_model=GenericResponse[RiskBlockerDetail],
    summary="Create a risk/blocker",
    description="Manually create a new risk or blocker for a deal.",
    dependencies=[Depends(rate_limit_user_write)],
)
async def create_risk_blocker(
    deal_id: UUID,
    body: RiskBlockerCreateRequest,
    response: FastAPIResponse,
    service: RiskBlockerService = Depends(get_risk_blocker_service),
) -> GenericResponse[RiskBlockerDetail]:
    """
    Create a new risk or blocker manually.

    Manual items start with status=OPEN (unlike AI-suggested items
    which start as AI_SUGGESTED and need confirmation).
    """
    try:
        item = await service.create(
            deal_id=deal_id,
            item_type=body.item_type,
            title=body.title,
            description=body.description,
            category=body.category,
            severity=body.severity,
            owner_name=body.owner_name,
            owner_email=body.owner_email,
            assigned_team=body.assigned_team,
        )

        return Response.created_response(
            message="Risk/blocker created successfully",
            data=item,
            response=response,
        )

    except RiskBlockerServiceError as e:
        logger.warning(f"Risk/blocker service error: {e.message}")
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception("Failed to create risk/blocker")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


# ─────────────────────────────────────────────────────────────────────────────
# Update Endpoints
# ─────────────────────────────────────────────────────────────────────────────


@risks_blockers_router.patch(
    "/risks-blockers/{risk_blocker_id}",
    response_model=GenericResponse[RiskBlockerDetail],
    summary="Update a risk/blocker",
    description="Update fields of a risk/blocker. All changes are tracked in the audit trail.",
    dependencies=[Depends(rate_limit_user_write)],
)
async def update_risk_blocker(
    risk_blocker_id: UUID,
    body: RiskBlockerUpdateRequest,
    response: FastAPIResponse,
    service: RiskBlockerService = Depends(get_risk_blocker_service),
) -> GenericResponse[RiskBlockerDetail]:
    """
    Update a risk/blocker.

    All changes are tracked in the audit trail with old/new values.
    """
    try:
        updates = body.model_dump(exclude_none=True)
        item = await service.update(risk_blocker_id, updates)

        if not item:
            return Response.error_response(
                message="Risk/blocker not found", status_code=404, response=response
            )

        return Response.success_response(
            message="Risk/blocker updated successfully",
            data=item,
        )

    except RiskBlockerServiceError as e:
        logger.warning(f"Risk/blocker service error: {e.message}")
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception("Failed to update risk/blocker")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


# ─────────────────────────────────────────────────────────────────────────────
# Status Transition Endpoints
# ─────────────────────────────────────────────────────────────────────────────


@risks_blockers_router.post(
    "/risks-blockers/{risk_blocker_id}/confirm",
    response_model=GenericResponse[RiskBlockerDetail],
    summary="Confirm an AI-suggested risk/blocker",
    description="Confirm an AI-suggested item, moving it from AI_SUGGESTED to OPEN status.",
    dependencies=[Depends(rate_limit_user_write)],
)
async def confirm_risk_blocker(
    risk_blocker_id: UUID,
    response: FastAPIResponse,
    service: RiskBlockerService = Depends(get_risk_blocker_service),
) -> GenericResponse[RiskBlockerDetail]:
    """
    Confirm an AI-suggested risk/blocker.

    This moves the item from AI_SUGGESTED to OPEN status,
    indicating the user has reviewed and accepted the AI's suggestion.
    """
    try:
        item = await service.confirm(risk_blocker_id)

        return Response.success_response(
            message="Risk/blocker confirmed successfully",
            data=item,
        )

    except RiskBlockerNotFoundError:
        return Response.error_response(
            message="Risk/blocker not found", status_code=404, response=response
        )
    except InvalidStatusTransitionError as e:
        return Response.error_response(message=e.message, status_code=400, response=response)
    except RiskBlockerServiceError as e:
        logger.warning(f"Risk/blocker service error: {e.message}")
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception("Failed to confirm risk/blocker")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@risks_blockers_router.post(
    "/risks-blockers/{risk_blocker_id}/dismiss",
    response_model=GenericResponse[RiskBlockerDetail],
    summary="Dismiss a risk/blocker",
    description="Dismiss a risk/blocker (e.g., not relevant, false positive).",
    dependencies=[Depends(rate_limit_user_write)],
)
async def dismiss_risk_blocker(
    risk_blocker_id: UUID,
    response: FastAPIResponse,
    service: RiskBlockerService = Depends(get_risk_blocker_service),
) -> GenericResponse[RiskBlockerDetail]:
    """
    Dismiss a risk/blocker.

    Use this for items that are not relevant or false positives.
    The item is moved to DISMISSED status.
    """
    try:
        item = await service.dismiss(risk_blocker_id)

        return Response.success_response(
            message="Risk/blocker dismissed successfully",
            data=item,
        )

    except RiskBlockerNotFoundError:
        return Response.error_response(
            message="Risk/blocker not found", status_code=404, response=response
        )
    except RiskBlockerServiceError as e:
        logger.warning(f"Risk/blocker service error: {e.message}")
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception("Failed to dismiss risk/blocker")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@risks_blockers_router.post(
    "/risks-blockers/{risk_blocker_id}/resolve",
    response_model=GenericResponse[RiskBlockerDetail],
    summary="Resolve a risk/blocker",
    description="Mark a risk/blocker as resolved with resolution notes.",
    dependencies=[Depends(rate_limit_user_write)],
)
async def resolve_risk_blocker(
    risk_blocker_id: UUID,
    body: RiskBlockerResolveRequest,
    response: FastAPIResponse,
    service: RiskBlockerService = Depends(get_risk_blocker_service),
) -> GenericResponse[RiskBlockerDetail]:
    """
    Resolve a risk/blocker.

    Marks the item as resolved and records resolution notes explaining
    how the issue was addressed.
    """
    try:
        item = await service.resolve(risk_blocker_id, body.resolution_notes)

        return Response.success_response(
            message="Risk/blocker resolved successfully",
            data=item,
        )

    except RiskBlockerNotFoundError:
        return Response.error_response(
            message="Risk/blocker not found", status_code=404, response=response
        )
    except RiskBlockerServiceError as e:
        logger.warning(f"Risk/blocker service error: {e.message}")
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception("Failed to resolve risk/blocker")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@risks_blockers_router.post(
    "/risks-blockers/{risk_blocker_id}/reopen",
    response_model=GenericResponse[RiskBlockerDetail],
    summary="Reopen a risk/blocker",
    description="Reopen a previously resolved or dismissed risk/blocker.",
    dependencies=[Depends(rate_limit_user_write)],
)
async def reopen_risk_blocker(
    risk_blocker_id: UUID,
    response: FastAPIResponse,
    service: RiskBlockerService = Depends(get_risk_blocker_service),
) -> GenericResponse[RiskBlockerDetail]:
    """
    Reopen a resolved or dismissed risk/blocker.

    Clears resolution data and moves the item back to OPEN status.
    """
    try:
        item = await service.reopen(risk_blocker_id)

        return Response.success_response(
            message="Risk/blocker reopened successfully",
            data=item,
        )

    except RiskBlockerNotFoundError:
        return Response.error_response(
            message="Risk/blocker not found", status_code=404, response=response
        )
    except InvalidStatusTransitionError as e:
        return Response.error_response(message=e.message, status_code=400, response=response)
    except RiskBlockerServiceError as e:
        logger.warning(f"Risk/blocker service error: {e.message}")
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception("Failed to reopen risk/blocker")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


# ─────────────────────────────────────────────────────────────────────────────
# Delete Endpoint
# ─────────────────────────────────────────────────────────────────────────────


@risks_blockers_router.delete(
    "/risks-blockers/{risk_blocker_id}",
    response_model=GenericResponse[None],
    summary="Delete a risk/blocker",
    description="Permanently delete a risk/blocker.",
    dependencies=[Depends(rate_limit_user_write)],
)
async def delete_risk_blocker(
    risk_blocker_id: UUID,
    response: FastAPIResponse,
    service: RiskBlockerService = Depends(get_risk_blocker_service),
) -> GenericResponse[None]:
    """
    Delete a risk/blocker.

    This permanently removes the item. Consider using dismiss or resolve
    instead to preserve the audit trail.
    """
    try:
        await service.delete(risk_blocker_id)

        return Response.success_response(
            message="Risk/blocker deleted successfully",
            data=None,
        )

    except RiskBlockerNotFoundError:
        return Response.error_response(
            message="Risk/blocker not found", status_code=404, response=response
        )
    except RiskBlockerServiceError as e:
        logger.warning(f"Risk/blocker service error: {e.message}")
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception("Failed to delete risk/blocker")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


# ─────────────────────────────────────────────────────────────────────────────
# NOTE: Extraction is backend-triggered only (event-driven)
# No frontend trigger endpoint - AI extraction runs automatically after
# new meetings/emails are synced. Users can still add risks/blockers manually.
# ─────────────────────────────────────────────────────────────────────────────
