"""CRM Stakeholders API handler.

This handler demonstrates the clean pattern for FastAPI responses:
- Returns GenericResponse directly (FastAPI validates via response_model)
- Uses Response parameter to set HTTP status codes for errors
- response_model provides OpenAPI docs and runtime validation
"""

from __future__ import annotations

from typing import Any, TypeVar, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi import Response as FastAPIResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.routers.crm.contacts_handler import get_contact_read_service
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.crm_models import (
    AvailableContactItem,
    CreateStakeholderRequest,
    CreateStakeholderResponse,
    DealJobResponse,
    StakeholderConflictResponse,
    StakeholderDetail,
    StakeholderFullDetail,
    StakeholderListResponse,
    UpdateStakeholderRequest,
)
from src.api.utils.http_response import Response
from src.contracts.crm.stakeholders import (
    CreateStakeholderCommand,
    UpdateStakeholderCommand,
)
from src.infra.utils.logger import get_logger
from src.service.crm.access.record_access_policy import CRMAccessActor
from src.service.crm.contacts.read_service import ContactReadService
from src.service.crm.insights.stakeholders_service import (
    StakeholdersService,
    StakeholdersServiceError,
)
from src.service.crm.jobs.deal_job_service import DealJobService
from src.service.crm.jobs.stakeholder_job_visibility_service import (
    StakeholderJobVisibilityService,
)
from src.service.crm.participants.contracts import StakeholderWriteContext
from src.service.crm.participants.stakeholder_read_service import StakeholderReadService
from src.service.crm.participants.stakeholder_write_service import StakeholderWriteService

logger = get_logger(__name__)

# Common error message (Sonar S1192 compliance)
_UNEXPECTED_ERR_MSG = "An unexpected error occurred while processing your request"

stakeholders_router = APIRouter()
ApiModelT = TypeVar("ApiModelT", bound=BaseModel)


def _to_api_model(model_type: type[ApiModelT], value: Any) -> ApiModelT:
    """Normalize service DTOs and ORM-like fakes into the API response model."""
    if isinstance(value, model_type):
        return value
    if isinstance(value, BaseModel):
        return model_type.model_validate(value.model_dump())
    return model_type.model_validate(value)


def _enum_or_string_value(value: object) -> str:
    """Return stable API enum text from either ORM strings or Enum instances."""
    enum_value = getattr(value, "value", value)
    return str(enum_value)


def _to_deal_job_response(job: Any) -> DealJobResponse:
    """Serialize a DealJobs-like object without assuming enum hydration mode."""
    return DealJobResponse(
        job_id=job.job_id,
        job_type=_enum_or_string_value(job.job_type),
        status=_enum_or_string_value(job.status),
    )


async def _contact_research_job_response(
    *,
    organisation_id: UUID,
    contact_id: UUID | None,
    user_id: UUID | None,
    force_refresh: bool = False,
) -> DealJobResponse | None:
    """Dispatch canonical contact research for a contact-backed stakeholder."""
    try:
        from src.workers.deal_worker.utils.contact_research_dispatch import (
            dispatch_contact_research_for_contact,
        )

        job_id = await dispatch_contact_research_for_contact(
            organisation_id=organisation_id,
            contact_id=contact_id,
            user_id=user_id,
            trigger="stakeholder_created",
            force_refresh=force_refresh,
        )
        if not job_id:
            return None
        return DealJobResponse(
            job_id=UUID(job_id),
            job_type="contact_research",
            status="pending",
        )
    except Exception:
        logger.error(
            "Failed to dispatch contact research",
            extra={
                "organisation_id": str(organisation_id),
                "contact_id": str(contact_id) if contact_id else None,
            },
            exc_info=True,
        )
        return None


async def get_stakeholders_service(
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency that yields a StakeholdersService instance for the current org."""
    service = StakeholdersService(session, user_ids.organisation_id, user_ids.user_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("Stakeholders service close failed", exc_info=True)


async def get_stakeholder_read_service(
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency that yields participant-backed stakeholder reads."""
    service = StakeholderReadService(
        session,
        user_ids.organisation_id,
        user_ids.user_id,
        CRMAccessActor(user_ids.user_id, user_ids.user_role, user_ids.user_email),
    )
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("Stakeholder read service close failed", exc_info=True)


async def get_stakeholder_write_service(
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency that yields participant-backed stakeholder semantic writes."""
    service = StakeholderWriteService(
        session,
        user_ids.organisation_id,
        user_ids.user_id,
        CRMAccessActor(user_ids.user_id, user_ids.user_role, user_ids.user_email),
    )
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("Stakeholder write service close failed", exc_info=True)


async def get_stakeholder_job_visibility_service(
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency that yields stakeholder job visibility orchestration."""
    service = StakeholderJobVisibilityService(
        session,
        user_ids.organisation_id,
        user_ids.user_id,
    )
    yield service


async def get_stakeholder_deal_job_service(
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    """Dependency that yields deal job lookup service for stakeholder routes."""
    service = DealJobService(session, user_ids.organisation_id, user_ids.user_id)
    try:
        yield service
    finally:
        await service.close()


@stakeholders_router.get(
    "/{deal_id}/stakeholders",
    response_model=GenericResponse[StakeholderListResponse],
    summary="Get stakeholders for a deal",
)
async def get_deal_stakeholders(
    deal_id: UUID,
    response: FastAPIResponse,
    include_pending: bool = Query(True, description="Legacy filter; ignored for new writes"),
    include_rejected: bool = Query(False, description="Legacy filter for old imported rows"),
    service: StakeholderReadService = Depends(get_stakeholder_read_service),
) -> GenericResponse[StakeholderListResponse]:
    """Get stakeholders associated with a specific deal.

    New stakeholder writes create active participants directly. The legacy
    approval filters remain for older imported/discovered rows during rollout.

    Response includes:
    - company_domain: For frontend to validate new stakeholder email domains
    - company_name: For display purposes
    - stakeholders: List of active stakeholder participants
    """
    try:
        result = await service.list_stakeholders_by_deal(
            deal_id,
            include_pending=include_pending,
            include_rejected=include_rejected,
        )
        return Response.success_response(
            message="Stakeholders retrieved",
            data=_to_api_model(StakeholderListResponse, result),
        )
    except StakeholdersServiceError as e:
        return Response.error_response(
            message=e.message, status_code=e.status_code, response=response
        )
    except Exception:
        logger.error("Error fetching stakeholders", extra={"deal_id": str(deal_id)}, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@stakeholders_router.get(
    "/{deal_id}/stakeholders/available-contacts",
    response_model=GenericResponse[list[AvailableContactItem]],
    summary="Search CRM contacts for the add-stakeholder picker",
)
async def list_available_contacts(
    deal_id: UUID,
    response: FastAPIResponse,
    query: str | None = Query(None, description="Legacy search query parameter"),
    q: str | None = Query(None, description="Search by name, email, or company"),
    limit: int = Query(20, ge=1, le=50, description="Maximum contacts to return"),
    service: ContactReadService = Depends(get_contact_read_service),
) -> GenericResponse[list[AvailableContactItem]]:
    """Compatibility endpoint backed by the canonical CRM contact search."""
    try:
        rows, _next_cursor = await service.search_contacts(
            q=q or query,
            limit=limit,
            cursor=None,
            company_id=None,
        )
        items = []
        for row in rows:
            email = row.get("email")
            if not email:
                continue
            company = row.get("company") or {}
            items.append(
                AvailableContactItem(
                    contact_id=row["contact_id"],
                    email=email,
                    first_name=None,
                    last_name=None,
                    full_name=row.get("full_name"),
                    job_title=row.get("job_title"),
                    company_name=company.get("name") if isinstance(company, dict) else None,
                    phone=None,
                    lifecycle_stage=None,
                    last_activity_at=None,
                )
            )
        return Response.success_response(message="Available contacts retrieved", data=items)
    except Exception:
        logger.exception("Unexpected error searching available contacts for deal %s", deal_id)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@stakeholders_router.post(
    "/{deal_id}/stakeholders",
    response_model=GenericResponse[CreateStakeholderResponse | StakeholderConflictResponse],
    summary="Manually add a new stakeholder",
)
async def create_stakeholder(
    deal_id: UUID,
    request: CreateStakeholderRequest,
    response: FastAPIResponse,
    user_ids: UserIDs = Depends(get_current_user),
    service: StakeholderWriteService = Depends(get_stakeholder_write_service),
    job_visibility_service: StakeholderJobVisibilityService = Depends(
        get_stakeholder_job_visibility_service
    ),
) -> GenericResponse[CreateStakeholderResponse | StakeholderConflictResponse]:
    """Manually add a new stakeholder to a deal.

    Use this endpoint when you want to add a stakeholder who is not in your CRM contacts.
    The stakeholder will be created with source=MANUAL and approval_status=APPROVED.

    Request Fields:
    - Send only contact_id, name, email, and/or linkedin_url.
    - At least one of contact_id, name, email, or linkedin_url must be non-empty.
    - Profile, buying-role, and engagement fields are filled by contact resolution
      and enrichment after creation.

    The request schema still accepts legacy optional fields during rollout, but
    frontend create forms should not send them.
    """
    try:
        command = CreateStakeholderCommand.model_validate(request.model_dump())
        stakeholder_result = await service.create_stakeholder(
            deal_id,
            command,
            context=StakeholderWriteContext(
                source="api",
                actor_user_id=user_ids.user_id,
                source_label="API",
                created_via="stakeholder_api",
            ),
        )
        await service.session.commit()
        stakeholder = _to_api_model(StakeholderDetail, stakeholder_result)
        processing_jobs = []
        research_job = None
        if stakeholder.email:
            processing_jobs = await job_visibility_service.create_stakeholder_processing_jobs(
                deal_id,
                stakeholder.participant_id,
            )
        research_job = await _contact_research_job_response(
            organisation_id=service.organisation_id,
            contact_id=stakeholder.contact_id,
            user_id=user_ids.user_id,
        )

        return Response.created_response(
            message="Stakeholder created successfully",
            data=CreateStakeholderResponse(
                stakeholder=stakeholder,
                processing_jobs=[_to_deal_job_response(job) for job in processing_jobs],
                research_job=research_job,
            ),
            response=response,
        )
    except StakeholdersServiceError as e:
        await service.session.rollback()
        logger.warning(
            "Create stakeholder error",
            extra={
                "organisation_id": str(service.organisation_id),
                "deal_id": str(deal_id),
                "failure_reason": e.message,
            },
        )
        return Response.error_response(
            message=e.message,
            status_code=e.status_code,
            data=cast(CreateStakeholderResponse | StakeholderConflictResponse | None, e.data),
            response=response,
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
    "/{deal_id}/stakeholders/{participant_id}",
    response_model=GenericResponse[StakeholderFullDetail],
    summary="Get stakeholder detail with engagement history",
)
async def get_stakeholder_detail(
    deal_id: UUID,
    participant_id: UUID,
    response: FastAPIResponse,
    service: StakeholderReadService = Depends(get_stakeholder_read_service),
    deal_job_service: DealJobService = Depends(get_stakeholder_deal_job_service),
) -> GenericResponse[StakeholderFullDetail]:
    """Get full details of a specific stakeholder including engagement history.

    This endpoint returns comprehensive stakeholder information including:
    - All stakeholder fields (name, job_title, department, etc.)
    - Current deal context (stance, temperature, decision_power)
    - Historical engagement snapshots showing how metrics changed over time

    The engagement_history field contains up to 15 snapshots (newest first)
    tracking changes in stance, temperature, and decision_power. This allows
    visualizing the stakeholder's journey
    (e.g., from COLD to WARM to HOT).
    """
    try:
        detail = await service.get_stakeholder_detail(deal_id, participant_id)
        if not detail:
            return Response.error_response(
                message="Stakeholder not found", status_code=404, response=response
            )
        contact_id = detail.get("contact_id") if isinstance(detail, dict) else None
        detail["active_research_job"] = (
            await deal_job_service.get_active_contact_research_job(UUID(str(contact_id)))
            if contact_id
            else None
        )
        return Response.success_response(
            message="Stakeholder detail retrieved",
            data=cast(StakeholderFullDetail, detail),
        )
    except StakeholdersServiceError as e:
        logger.warning(
            "Get stakeholder detail error",
            extra={
                "organisation_id": str(service.organisation_id),
                "deal_id": str(deal_id),
                "participant_id": str(participant_id),
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
                "participant_id": str(participant_id),
            },
        )
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@stakeholders_router.patch(
    "/{deal_id}/stakeholders/{participant_id}",
    response_model=GenericResponse[StakeholderDetail],
    summary="Edit stakeholder details",
)
async def update_stakeholder(
    deal_id: UUID,
    participant_id: UUID,
    request: UpdateStakeholderRequest,
    response: FastAPIResponse,
    service: StakeholderWriteService = Depends(get_stakeholder_write_service),
) -> GenericResponse[StakeholderDetail]:
    """Partially update editable stakeholder fields."""
    try:
        command = UpdateStakeholderCommand.model_validate(request.model_dump(exclude_unset=True))
        stakeholder_result = await service.update_stakeholder(deal_id, participant_id, command)
        await service.session.commit()
        stakeholder = _to_api_model(StakeholderDetail, stakeholder_result)
        return Response.success_response(message="Stakeholder updated", data=stakeholder)
    except StakeholdersServiceError as e:
        await service.session.rollback()
        logger.warning(
            "Update stakeholder error",
            extra={
                "organisation_id": str(service.organisation_id),
                "deal_id": str(deal_id),
                "participant_id": str(participant_id),
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
                "participant_id": str(participant_id),
            },
        )
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@stakeholders_router.post(
    "/{deal_id}/stakeholders/{participant_id}/research",
    response_model=GenericResponse[DealJobResponse],
    summary="Trigger contact research for a stakeholder",
)
async def trigger_stakeholder_contact_research(
    deal_id: UUID,
    participant_id: UUID,
    response: FastAPIResponse,
    user_ids: UserIDs = Depends(get_current_user),
    service: StakeholderReadService = Depends(get_stakeholder_read_service),
) -> GenericResponse[DealJobResponse]:
    """Manually trigger contact-owned research for a stakeholder.

    Dispatches a research job for the linked CRM contact. Unlinked
    stakeholders must be resolved to contacts before research can run.

    Returns the job_id if dispatched, or a message if skipped.
    """
    try:
        # Verify stakeholder exists and belongs to this deal
        detail = await service.get_stakeholder_detail(deal_id, participant_id)
        if not detail:
            return Response.error_response(
                message="Stakeholder not found", status_code=404, response=response
            )

        contact_id = detail.get("contact_id") if isinstance(detail, dict) else None
        if not contact_id:
            return Response.error_response(
                message="Stakeholder must be linked to a contact before research can run",
                status_code=400,
                response=response,
            )

        job = await _contact_research_job_response(
            organisation_id=service.organisation_id,
            contact_id=UUID(str(contact_id)),
            user_id=user_ids.user_id,
            force_refresh=True,
        )

        if job:
            logger.info(
                "Contact research triggered from stakeholder",
                extra={
                    "event": "stakeholder_contact_research_triggered",
                    "organisation_id": str(service.organisation_id),
                    "participant_id": str(participant_id),
                    "deal_id": str(deal_id),
                    "job_id": str(job.job_id),
                    "user_id": str(user_ids.user_id),
                },
            )
            return Response.success_response(message="Research job dispatched", data=job)

        return Response.success_response(
            message="Research skipped — already researched recently",
            data=DealJobResponse(
                job_id=None,
                job_type="contact_research",
                status="completed",
            ),
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
                "participant_id": str(participant_id),
                "deal_id": str(deal_id),
            },
        )
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@stakeholders_router.delete(
    "/{deal_id}/stakeholders/{participant_id}",
    response_model=GenericResponse[StakeholderDetail],
    summary="Remove a stakeholder from the deal",
)
async def remove_stakeholder(
    deal_id: UUID,
    participant_id: UUID,
    response: FastAPIResponse,
    service: StakeholderWriteService = Depends(get_stakeholder_write_service),
) -> GenericResponse[StakeholderDetail]:
    """Remove a stakeholder from the deal.

    This endpoint soft-deletes the participant, excluding them from:
    - Deal stakeholder lists (unless explicitly requested)
    - Calendar event matching
    - Email matching
    - Stakeholder analysis
    """
    try:
        stakeholder = await service.remove_stakeholder(deal_id, participant_id)
        await service.session.commit()
        return Response.success_response(
            message="Stakeholder removed",
            data=cast(StakeholderDetail, stakeholder),
        )
    except StakeholdersServiceError as e:
        await service.session.rollback()
        logger.warning(
            "Remove stakeholder error",
            extra={
                "organisation_id": str(service.organisation_id),
                "deal_id": str(deal_id),
                "participant_id": str(participant_id),
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
                "participant_id": str(participant_id),
            },
        )
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )
