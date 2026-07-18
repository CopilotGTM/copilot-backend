"""Risks & Blockers API handler for managing deal risks and blockers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi import Response as FastAPIResponse
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.crm_record_access import access_actor_from_user
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
    RiskBlockerListQueryParams,
    RiskBlockerListResponse,
    RiskBlockerResolveRequest,
    RiskBlockerUpdateRequest,
)
from src.api.utils.http_response import Response
from src.domain.models.organisation_tables.sync.risk_blocker_models import (
    RiskBlockerStatus,
)
from src.infra.utils.logger import get_logger
from src.service.crm.access.record_access_policy import (
    CRMRecordAccessError,
    DealAccessLevel,
    DealAccessPolicy,
)
from src.service.crm.insights.risk_blocker_service import (
    InvalidStatusTransitionError,
    RiskBlockerNotFoundError,
    RiskBlockerServiceError,
)
from src.service.crm.risks.contracts import RiskBlockerActor, RiskBlockerWriteContext
from src.service.crm.risks.lifecycle_service import RiskBlockerLifecycleService
from src.service.crm.risks.read_service import RiskBlockerReadService
from src.service.crm.risks.write_service import RiskBlockerWriteService

logger = get_logger("risks_blockers_handler")

_UNEXPECTED_ERR_MSG = "An unexpected error occurred while processing your request"

risks_blockers_router = APIRouter()


@dataclass
class RiskBlockerRequestServices:
    read: RiskBlockerReadService
    write: RiskBlockerWriteService
    lifecycle: RiskBlockerLifecycleService
    session: AsyncSession
    context: RiskBlockerWriteContext
    access: DealAccessPolicy

    async def close(self) -> None:
        await self.session.close()


async def get_risk_blocker_service(
    user: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency that yields request-scoped canonical risk/blocker services."""
    context = RiskBlockerWriteContext(
        source="api",
        actor=RiskBlockerActor(
            actor_type="user",
            actor_id=user.user_id,
            actor_name=user.user_name if hasattr(user, "user_name") else None,
        ),
    )
    service = RiskBlockerRequestServices(
        read=RiskBlockerReadService(session, user.organisation_id),
        write=RiskBlockerWriteService(session, user.organisation_id),
        lifecycle=RiskBlockerLifecycleService(session, user.organisation_id),
        session=session,
        context=context,
        access=DealAccessPolicy(
            session,
            user.organisation_id,
            access_actor_from_user(user),
        ),
    )
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("RiskBlockerService close failed", exc_info=True)


def _read_service(service: Any) -> Any:
    return service.read if hasattr(service, "read") else service


def _write_service(service: Any) -> Any:
    return service.write if hasattr(service, "write") else service


def _lifecycle_service(service: Any) -> Any:
    return service.lifecycle if hasattr(service, "lifecycle") else service


def _write_context(service: Any) -> RiskBlockerWriteContext | None:
    return service.context if hasattr(service, "context") else None


async def _commit_if_owned(service: Any) -> None:
    if hasattr(service, "session"):
        await service.session.commit()


async def _rollback_if_owned(service: Any) -> None:
    if hasattr(service, "session"):
        await service.session.rollback()


async def _require_risk_access(
    service: Any,
    risk_blocker_id: UUID,
    level: DealAccessLevel,
) -> Any:
    """Authorize a risk through its parent deal before returning or mutating it."""
    item = await _read_service(service).get_by_id(risk_blocker_id)
    if item is None:
        raise RiskBlockerNotFoundError("Risk/blocker not found")
    if hasattr(service, "access"):
        try:
            await service.access.require_deal(item.deal_id, level)
        except CRMRecordAccessError as exc:
            raise RiskBlockerServiceError(exc.message, status_code=exc.status_code) from exc
    return item


async def _require_risk_deal_access(
    service: Any,
    deal_id: UUID,
    level: DealAccessLevel,
) -> None:
    """Authorize deal-addressed risk operations before their child query executes."""
    if hasattr(service, "access"):
        try:
            await service.access.require_deal(deal_id, level)
        except CRMRecordAccessError as exc:
            raise RiskBlockerServiceError(exc.message, status_code=exc.status_code) from exc


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
    service: Any = Depends(get_risk_blocker_service),
    query: RiskBlockerListQueryParams = Depends(),
) -> GenericResponse[RiskBlockerListResponse]:
    """
    Get risks and blockers for a deal.

    Returns a paginated list sorted by creation date. AI-extracted and
    user-created items both land directly in the active list with status=OPEN.

    By default, resolved and dismissed items are excluded. Use include_resolved=true
    to see all items including closed ones.

    Query parameters:
    - limit: Number of items per page (1-100, default 20)
    - page: Page number (1-indexed)
    - item_type: Filter by RISK or BLOCKER
    - status: Comma-separated status filter (OPEN,IN_PROGRESS,RESOLVED,etc.)
    - severity: Filter by severity level (CRITICAL,HIGH,MEDIUM,LOW)
    - category: Filter by category (TECHNICAL,BUDGET,COMPETITION,etc.)
    - include_resolved: Include resolved/dismissed items (default false)
    - sort: Sort order (desc = newest first, asc = oldest first)
    """
    try:
        await _require_risk_deal_access(service, deal_id, "view")
        # Parse status filter
        parsed_statuses = None
        if query.status:
            status_strs = [s.strip().upper() for s in query.status.split(",")]
            parsed_statuses = []
            for s in status_strs:
                try:
                    parsed_statuses.append(RiskBlockerStatus(s))
                except ValueError:
                    logger.warning(f"Invalid status filter: {s}")

        items, total_count, summary = await _read_service(service).list_for_deal(
            deal_id=deal_id,
            limit=query.limit,
            page=query.page,
            item_type=query.item_type,
            statuses=parsed_statuses,
            severity=query.severity,
            category=query.category,
            include_resolved=query.include_resolved,
            sort_order=query.sort,
        )

        total_pages = (total_count + query.limit - 1) // query.limit if query.limit else 0

        return Response.success_response(
            message="Risks and blockers retrieved successfully",
            data=RiskBlockerListResponse(
                deal_id=deal_id,
                items=items,
                page=query.page,
                limit=query.limit,
                total_count=total_count,
                total_pages=total_pages,
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
    service: Any = Depends(get_risk_blocker_service),
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
        item = await _require_risk_access(service, risk_blocker_id, "view")

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
    service: Any = Depends(get_risk_blocker_service),
) -> GenericResponse[RiskBlockerDetail]:
    """
    Create a new risk or blocker manually.

    Items are created directly with status=OPEN.
    """
    try:
        await _require_risk_deal_access(service, deal_id, "collaborate")
        context = _write_context(service)
        if context:
            item = await _write_service(service).create_manual(
                deal_id=deal_id,
                item_type=body.item_type,
                title=body.title,
                context=context,
                description=body.description,
                category=body.category,
                severity=body.severity,
                owner_name=body.owner_name,
                owner_email=body.owner_email,
                assigned_team=body.assigned_team,
            )
            await _commit_if_owned(service)
        else:
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
        await _rollback_if_owned(service)
        logger.warning(f"Risk/blocker service error: {e.message}")
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        await _rollback_if_owned(service)
        logger.exception("Failed to create risk/blocker")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


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
    service: Any = Depends(get_risk_blocker_service),
) -> GenericResponse[RiskBlockerDetail]:
    """
    Update a risk/blocker.

    All changes are tracked in the audit trail with old/new values.
    """
    try:
        await _require_risk_access(service, risk_blocker_id, "collaborate")
        updates = body.model_dump(exclude_none=True)
        context = _write_context(service)
        if context:
            item = await _write_service(service).update_fields(risk_blocker_id, updates, context)
            if item:
                await _commit_if_owned(service)
        else:
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
        await _rollback_if_owned(service)
        logger.warning(f"Risk/blocker service error: {e.message}")
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        await _rollback_if_owned(service)
        logger.exception("Failed to update risk/blocker")
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
    service: Any = Depends(get_risk_blocker_service),
) -> GenericResponse[RiskBlockerDetail]:
    """
    Dismiss a risk/blocker.

    Use this for items that are not relevant or false positives.
    The item is moved to DISMISSED status.
    """
    try:
        await _require_risk_access(service, risk_blocker_id, "collaborate")
        context = _write_context(service)
        if context:
            item = await _lifecycle_service(service).dismiss(risk_blocker_id, context=context)
            await _commit_if_owned(service)
        else:
            item = await service.dismiss(risk_blocker_id)

        return Response.success_response(
            message="Risk/blocker dismissed successfully",
            data=item,
        )

    except RiskBlockerNotFoundError:
        await _rollback_if_owned(service)
        return Response.error_response(
            message="Risk/blocker not found", status_code=404, response=response
        )
    except RiskBlockerServiceError as e:
        await _rollback_if_owned(service)
        logger.warning(f"Risk/blocker service error: {e.message}")
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        await _rollback_if_owned(service)
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
    service: Any = Depends(get_risk_blocker_service),
) -> GenericResponse[RiskBlockerDetail]:
    """
    Resolve a risk/blocker.

    Marks the item as resolved and records resolution notes explaining
    how the issue was addressed.
    """
    try:
        await _require_risk_access(service, risk_blocker_id, "collaborate")
        context = _write_context(service)
        if context:
            item = await _lifecycle_service(service).resolve(
                risk_blocker_id,
                body.resolution_notes,
                context=context,
            )
            await _commit_if_owned(service)
        else:
            item = await service.resolve(risk_blocker_id, body.resolution_notes)

        return Response.success_response(
            message="Risk/blocker resolved successfully",
            data=item,
        )

    except RiskBlockerNotFoundError:
        await _rollback_if_owned(service)
        return Response.error_response(
            message="Risk/blocker not found", status_code=404, response=response
        )
    except RiskBlockerServiceError as e:
        await _rollback_if_owned(service)
        logger.warning(f"Risk/blocker service error: {e.message}")
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        await _rollback_if_owned(service)
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
    service: Any = Depends(get_risk_blocker_service),
) -> GenericResponse[RiskBlockerDetail]:
    """
    Reopen a resolved or dismissed risk/blocker.

    Clears resolution data and moves the item back to OPEN status.
    """
    try:
        await _require_risk_access(service, risk_blocker_id, "collaborate")
        context = _write_context(service)
        if context:
            item = await _lifecycle_service(service).reopen(risk_blocker_id, context=context)
            await _commit_if_owned(service)
        else:
            item = await service.reopen(risk_blocker_id)

        return Response.success_response(
            message="Risk/blocker reopened successfully",
            data=item,
        )

    except RiskBlockerNotFoundError:
        await _rollback_if_owned(service)
        return Response.error_response(
            message="Risk/blocker not found", status_code=404, response=response
        )
    except InvalidStatusTransitionError as e:
        await _rollback_if_owned(service)
        return Response.error_response(message=e.message, status_code=400, response=response)
    except RiskBlockerServiceError as e:
        await _rollback_if_owned(service)
        logger.warning(f"Risk/blocker service error: {e.message}")
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        await _rollback_if_owned(service)
        logger.exception("Failed to reopen risk/blocker")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


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
    service: Any = Depends(get_risk_blocker_service),
) -> GenericResponse[None]:
    """
    Delete a risk/blocker.

    This permanently removes the item. Consider using dismiss or resolve
    instead to preserve the audit trail.
    """
    try:
        await _require_risk_access(service, risk_blocker_id, "control")
        context = _write_context(service)
        if context:
            await _write_service(service).delete(risk_blocker_id)
            await _commit_if_owned(service)
        else:
            await service.delete(risk_blocker_id)

        return Response.success_response(
            message="Risk/blocker deleted successfully",
            data=None,
        )

    except RiskBlockerNotFoundError:
        await _rollback_if_owned(service)
        return Response.error_response(
            message="Risk/blocker not found", status_code=404, response=response
        )
    except RiskBlockerServiceError as e:
        await _rollback_if_owned(service)
        logger.warning(f"Risk/blocker service error: {e.message}")
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        await _rollback_if_owned(service)
        logger.exception("Failed to delete risk/blocker")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )
