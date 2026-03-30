"""CRM Stakeholders API handler.

This handler demonstrates the clean pattern for FastAPI responses:
- Returns GenericResponse directly (FastAPI validates via response_model)
- Uses Response parameter to set HTTP status codes for errors
- response_model provides OpenAPI docs and runtime validation
"""

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
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.crm_models import (
    CreateStakeholderRequest,
    StakeholderDetail,
    StakeholderFullDetail,
    StakeholderListResponse,
    UpdateStakeholderRequest,
)
from src.api.utils.http_response import Response
from src.infra.utils.logger import get_logger
from src.service.crm.insights.stakeholders_service import (
    StakeholdersService,
    StakeholdersServiceError,
)

logger = get_logger(__name__)

# Common error message (Sonar S1192 compliance)
_UNEXPECTED_ERR_MSG = "An unexpected error occurred while processing your request"

stakeholders_router = APIRouter()


async def get_stakeholders_service(
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency that yields a StakeholdersService instance for the current org."""
    service = StakeholdersService(session, user_ids.organisation_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("Stakeholders service close failed", exc_info=True)


@stakeholders_router.get(
    "/{deal_id}/stakeholders",
    response_model=GenericResponse[StakeholderListResponse],
    summary="Get stakeholders for a deal",
)
async def get_deal_stakeholders(
    deal_id: UUID,
    response: FastAPIResponse,
    include_pending: bool = Query(
        True, description="Include PENDING stakeholders awaiting approval"
    ),
    include_rejected: bool = Query(False, description="Include REJECTED stakeholders"),
    service: StakeholdersService = Depends(get_stakeholders_service),
) -> GenericResponse[StakeholderListResponse]:
    """Get stakeholders associated with a specific deal.

    By default returns APPROVED and PENDING stakeholders.
    Use query parameters to control which approval statuses are included.

    Response includes:
    - company_domain: For frontend to validate new stakeholder email domains
    - company_name: For display purposes
    - stakeholders: List of stakeholders filtered by approval_status

    The approval_status field indicates:
    - APPROVED: Stakeholder is active and used for deal matching
    - PENDING: Stakeholder discovered but awaiting user approval (> 5 per source)
    - REJECTED: Stakeholder excluded from deal matching by user
    """
    try:
        result = await service.list_stakeholders_by_deal(
            deal_id,
            include_pending=include_pending,
            include_rejected=include_rejected,
        )
        return Response.success_response(message="Stakeholders retrieved", data=result)
    except StakeholdersServiceError as e:
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception as e:
        logger.error("Error fetching stakeholders", extra={"deal_id": str(deal_id)}, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


# @stakeholders_router.get(
#     "/{deal_id}/stakeholders/available-contacts",
#     response_model=GenericResponse[list[AvailableContactItem]],
#     summary="List available contacts for adding as stakeholders",
# )
# async def list_available_contacts(
#     deal_id: UUID,
#     response: FastAPIResponse,
#     query: str | None = None,
#     limit: int = 50,
#     service: StakeholdersService = Depends(get_stakeholders_service),
# ) -> GenericResponse[list[AvailableContactItem]]:
#     """
#     List contacts that can be added as stakeholders to the deal.

#     Only returns contacts that have a valid email address, as email is required
#     for stakeholder identification and communication.

#     Response includes:
#     - Contact identity (name, email)
#     - Professional context (job title, company name)
#     - CRM metadata for reference
#     """
#     try:
#         contacts = await service.list_available_contacts(deal_id, query, limit)
#         return Response.success_response(message="Available contacts retrieved", data=contacts)
#     except StakeholdersServiceError as e:
#         logger.warning("Available contacts service error: %s", e.message)
#         return Response.error_response(
#             message=e.message, status_code=e.status_code, response=response
#         )
#     except Exception as e:
#         logger.error("Unexpected error listing available contacts: %s", e, exc_info=True)
#         return Response.error_response(
#             message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
#         )


# @stakeholders_router.post(
#     "/{deal_id}/stakeholders/add-from-contacts",
#     response_model=GenericResponse[AddContactsAsStakeholdersResponse],
#     summary="Add selected contacts as stakeholders",
# )
# async def add_contacts_as_stakeholders(
#     deal_id: UUID,
#     request: AddContactsAsStakeholdersRequest,
#     response: FastAPIResponse,
#     service: StakeholdersService = Depends(get_stakeholders_service),
# ) -> GenericResponse[AddContactsAsStakeholdersResponse]:
#     """
#     Add selected contacts as stakeholders to a deal.

#     Only contacts with valid email addresses will be added. Contacts without
#     email addresses are skipped and reported in the response.

#     Returns:
#     - List of successfully added stakeholders
#     - List of skipped contact IDs (no email or already exists)
#     """
#     try:
#         result = await service.add_contacts_as_stakeholders(deal_id, request.contact_ids)
#         if result.total_added > 0:
#             await _trigger_deal_mapping_if_ready(service, deal_id)
#         return Response.success_response(message="Stakeholders added from contacts", data=result)
#     except StakeholdersServiceError as e:
#         logger.warning("Add contacts as stakeholders error: %s", e.message)
#         return Response.error_response(
#             message=e.message, status_code=e.status_code, response=response
#         )
#     except Exception:
#         logger.exception("Failed to add contacts as stakeholders for deal %s", deal_id)
#         return Response.error_response(
#             message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
#         )


@stakeholders_router.post(
    "/{deal_id}/stakeholders",
    response_model=GenericResponse[StakeholderDetail],
    summary="Manually add a new stakeholder",
)
async def create_stakeholder(
    deal_id: UUID,
    request: CreateStakeholderRequest,
    response: FastAPIResponse,
    service: StakeholdersService = Depends(get_stakeholders_service),
) -> GenericResponse[StakeholderDetail]:
    """Manually add a new stakeholder to a deal.

    Use this endpoint when you want to add a stakeholder who is not in your CRM contacts.
    The stakeholder will be created with source=MANUAL and approval_status=APPROVED.

    Request Fields:
    - REQUIRED: email (must be unique per deal)
    - PRIMARY: name, job_title, stance, decision_power
    - SECONDARY: department, champion_potential, motivation, pain_points, external_links

    The request schema includes UI hints (ui_category, ui_component) for frontend form building.
    Check the OpenAPI schema for field-level metadata.
    """
    try:
        stakeholder = await service.create_stakeholder(deal_id, request)
        await service.trigger_deal_mapping_if_ready(deal_id)

        # Dispatch research after mapping tasks are queued (ensures Celery client
        # is initialized and mapper tasks don't race with the research dispatch)
        try:
            from src.workers.deal_worker.utils.stakeholder_research_dispatch import (
                dispatch_stakeholder_research_async,
            )

            logger.info(
                "Stakeholder research dispatch requested",
                extra={
                    "event": "stakeholder_research_dispatch_requested",
                    "organisation_id": str(service.organisation_id),
                    "stakeholder_id": str(stakeholder.stakeholder_id),
                    "deal_id": str(deal_id),
                },
            )
            job_id = await dispatch_stakeholder_research_async(
                organisation_id=service.organisation_id,
                stakeholder_id=stakeholder.stakeholder_id,
                deal_id=deal_id,
            )
            logger.info(
                "Stakeholder research dispatch completed",
                extra={
                    "event": "stakeholder_research_dispatch_completed",
                    "organisation_id": str(service.organisation_id),
                    "stakeholder_id": str(stakeholder.stakeholder_id),
                    "deal_id": str(deal_id),
                    "job_id": job_id,
                },
            )
        except Exception:
            logger.error(
                "Failed to dispatch stakeholder research",
                extra={
                    "organisation_id": str(service.organisation_id),
                    "stakeholder_id": str(stakeholder.stakeholder_id),
                    "deal_id": str(deal_id),
                },
                exc_info=True,
            )

        return Response.created_response(
            message="Stakeholder created successfully",
            data=stakeholder,
            response=response,
        )
    except StakeholdersServiceError as e:
        logger.warning(
            "Create stakeholder error",
            extra={
                "organisation_id": str(service.organisation_id),
                "deal_id": str(deal_id),
                "failure_reason": e.message,
            },
        )
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception(
            "Failed to create stakeholder",
            extra={
                "organisation_id": str(service.organisation_id),
                "deal_id": str(deal_id),
            },
        )
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@stakeholders_router.get(
    "/{deal_id}/stakeholders/{stakeholder_id}",
    response_model=GenericResponse[StakeholderFullDetail],
    summary="Get stakeholder detail with engagement history",
)
async def get_stakeholder_detail(
    deal_id: UUID,
    stakeholder_id: UUID,
    response: FastAPIResponse,
    service: StakeholdersService = Depends(get_stakeholders_service),
) -> GenericResponse[StakeholderFullDetail]:
    """Get full details of a specific stakeholder including engagement history.

    This endpoint returns comprehensive stakeholder information including:
    - All stakeholder fields (name, job_title, department, etc.)
    - Current engagement metrics (stance, temperature, engagement_score)
    - Historical engagement snapshots showing how metrics changed over time

    The engagement_history field contains up to 15 snapshots (newest first)
    tracking changes in stance, temperature, engagement_score, champion_potential,
    and decision_power. This allows visualizing the stakeholder's journey
    (e.g., from COLD to WARM to HOT).
    """
    try:
        detail = await service.get_stakeholder_detail(deal_id, stakeholder_id)
        if not detail:
            return Response.error_response(
                message="Stakeholder not found", status_code=404, response=response
            )
        return Response.success_response(message="Stakeholder detail retrieved", data=detail)
    except StakeholdersServiceError as e:
        logger.warning(
            "Get stakeholder detail error",
            extra={
                "organisation_id": str(service.organisation_id),
                "deal_id": str(deal_id),
                "stakeholder_id": str(stakeholder_id),
                "failure_reason": e.message,
            },
        )
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception(
            "Failed to get stakeholder",
            extra={
                "organisation_id": str(service.organisation_id),
                "deal_id": str(deal_id),
                "stakeholder_id": str(stakeholder_id),
            },
        )
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@stakeholders_router.patch(
    "/{deal_id}/stakeholders/{stakeholder_id}",
    response_model=GenericResponse[StakeholderDetail],
    summary="Edit stakeholder details",
)
async def update_stakeholder(
    deal_id: UUID,
    stakeholder_id: UUID,
    request: UpdateStakeholderRequest,
    response: FastAPIResponse,
    service: StakeholdersService = Depends(get_stakeholders_service),
) -> GenericResponse[StakeholderDetail]:
    """Partially update editable stakeholder fields."""
    try:
        stakeholder = await service.update_stakeholder(deal_id, stakeholder_id, request)
        return Response.success_response(message="Stakeholder updated", data=stakeholder)
    except StakeholdersServiceError as e:
        logger.warning(
            "Update stakeholder error",
            extra={
                "organisation_id": str(service.organisation_id),
                "deal_id": str(deal_id),
                "stakeholder_id": str(stakeholder_id),
                "failure_reason": e.message,
            },
        )
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception(
            "Failed to update stakeholder",
            extra={
                "organisation_id": str(service.organisation_id),
                "deal_id": str(deal_id),
                "stakeholder_id": str(stakeholder_id),
            },
        )
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@stakeholders_router.post(
    "/{deal_id}/stakeholders/{stakeholder_id}/approve",
    response_model=GenericResponse[StakeholderDetail],
    summary="Approve a pending stakeholder",
)
async def approve_stakeholder(
    deal_id: UUID,
    stakeholder_id: UUID,
    response: FastAPIResponse,
    service: StakeholdersService = Depends(get_stakeholders_service),
) -> GenericResponse[StakeholderDetail]:
    """Approve a PENDING stakeholder for deal matching.

    Discovered stakeholders beyond the first 5 per source are marked as PENDING
    and require manual approval before they're used in deal matching.

    This endpoint changes the stakeholder's approval_status from PENDING to APPROVED.
    """
    try:
        stakeholder = await service.approve_stakeholder(deal_id, stakeholder_id)
        return Response.success_response(message="Stakeholder approved", data=stakeholder)
    except StakeholdersServiceError as e:
        logger.warning(
            "Approve stakeholder error",
            extra={
                "organisation_id": str(service.organisation_id),
                "deal_id": str(deal_id),
                "stakeholder_id": str(stakeholder_id),
                "failure_reason": e.message,
            },
        )
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception(
            "Failed to approve stakeholder",
            extra={
                "organisation_id": str(service.organisation_id),
                "deal_id": str(deal_id),
                "stakeholder_id": str(stakeholder_id),
            },
        )
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@stakeholders_router.post(
    "/{deal_id}/stakeholders/{stakeholder_id}/reject",
    response_model=GenericResponse[StakeholderDetail],
    summary="Reject a stakeholder",
)
async def reject_stakeholder(
    deal_id: UUID,
    stakeholder_id: UUID,
    response: FastAPIResponse,
    service: StakeholdersService = Depends(get_stakeholders_service),
) -> GenericResponse[StakeholderDetail]:
    """Reject a stakeholder to exclude from deal matching.

    Rejected stakeholders are not used for:
    - Calendar event matching
    - Email matching
    - Stakeholder analysis

    This can be used to remove incorrectly discovered stakeholders or
    contacts that are not relevant to the deal.
    """
    try:
        stakeholder = await service.reject_stakeholder(deal_id, stakeholder_id)
        return Response.success_response(message="Stakeholder rejected", data=stakeholder)
    except StakeholdersServiceError as e:
        logger.warning(
            "Reject stakeholder error",
            extra={
                "organisation_id": str(service.organisation_id),
                "deal_id": str(deal_id),
                "stakeholder_id": str(stakeholder_id),
                "failure_reason": e.message,
            },
        )
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception(
            "Failed to reject stakeholder",
            extra={
                "organisation_id": str(service.organisation_id),
                "deal_id": str(deal_id),
                "stakeholder_id": str(stakeholder_id),
            },
        )
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@stakeholders_router.post(
    "/{deal_id}/stakeholders/{stakeholder_id}/research",
    response_model=GenericResponse[dict],
    summary="Trigger stakeholder research",
)
async def trigger_stakeholder_research(
    deal_id: UUID,
    stakeholder_id: UUID,
    response: FastAPIResponse,
    user_ids: UserIDs = Depends(get_current_user),
    service: StakeholdersService = Depends(get_stakeholders_service),
) -> GenericResponse[dict]:
    """Manually trigger LinkedIn research for a stakeholder.

    Dispatches a research job to enrich the stakeholder's profile.
    If research was already run recently (within 30 days), it will be skipped.

    Returns the job_id if dispatched, or a message if skipped.
    """
    try:
        # Verify stakeholder exists and belongs to this deal
        detail = await service.get_stakeholder_detail(deal_id, stakeholder_id)
        if not detail:
            return Response.error_response(
                message="Stakeholder not found", status_code=404, response=response
            )

        from src.workers.deal_worker.utils.stakeholder_research_dispatch import (
            dispatch_stakeholder_research_async,
        )

        job_id = await dispatch_stakeholder_research_async(
            organisation_id=service.organisation_id,
            stakeholder_id=stakeholder_id,
            deal_id=deal_id,
            user_id=user_ids.user_id,
            force=True,
        )

        if job_id:
            logger.info(
                "Stakeholder research triggered",
                extra={
                    "event": "stakeholder_research_triggered",
                    "organisation_id": str(service.organisation_id),
                    "stakeholder_id": str(stakeholder_id),
                    "deal_id": str(deal_id),
                    "job_id": job_id,
                    "user_id": str(user_ids.user_id),
                },
            )
            return Response.success_response(
                message="Research job dispatched",
                data={"job_id": job_id},
            )

        return Response.success_response(
            message="Research skipped — already researched recently",
            data={"job_id": None},
        )
    except StakeholdersServiceError as e:
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception(
            "Failed to trigger research for stakeholder",
            extra={
                "organisation_id": str(service.organisation_id),
                "stakeholder_id": str(stakeholder_id),
                "deal_id": str(deal_id),
            },
        )
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@stakeholders_router.delete(
    "/{deal_id}/stakeholders/{stakeholder_id}",
    response_model=GenericResponse[StakeholderDetail],
    summary="Remove a stakeholder from the deal",
)
async def remove_stakeholder(
    deal_id: UUID,
    stakeholder_id: UUID,
    response: FastAPIResponse,
    service: StakeholdersService = Depends(get_stakeholders_service),
) -> GenericResponse[StakeholderDetail]:
    """Remove an approved stakeholder from the deal.

    This endpoint marks the stakeholder as REMOVED, excluding them from:
    - Deal stakeholder lists (unless explicitly requested)
    - Calendar event matching
    - Email matching
    - Stakeholder analysis

    Unlike rejection, removal is intended for stakeholders that were
    previously active but should no longer be associated with the deal.

    Only APPROVED stakeholders can be removed. Use the reject endpoint
    for PENDING stakeholders.
    """
    try:
        stakeholder = await service.remove_stakeholder(deal_id, stakeholder_id)
        return Response.success_response(message="Stakeholder removed", data=stakeholder)
    except StakeholdersServiceError as e:
        logger.warning(
            "Remove stakeholder error",
            extra={
                "organisation_id": str(service.organisation_id),
                "deal_id": str(deal_id),
                "stakeholder_id": str(stakeholder_id),
                "failure_reason": e.message,
            },
        )
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.exception(
            "Failed to remove stakeholder",
            extra={
                "organisation_id": str(service.organisation_id),
                "deal_id": str(deal_id),
                "stakeholder_id": str(stakeholder_id),
            },
        )
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )
