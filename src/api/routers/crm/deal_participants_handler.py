"""CRM deal participants API handler."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi import Response as FastAPIResponse
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.dependencies.crm_write_guard import require_crm_write_scope
from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_write
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.crm_participant_models import (
    DealParticipantItem,
    DealParticipantsCreateRequest,
    DealParticipantSkippedItem,
    DealParticipantsWriteResponse,
    DealParticipantUpdateRequest,
)
from src.api.utils.http_response import Response
from src.infra.utils.logger import get_logger
from src.service.crm.internal.deal_participant_service import (
    DealParticipantService,
    DealParticipantServiceError,
)

logger = get_logger(__name__)

_UNEXPECTED_ERR_MSG = "An unexpected error occurred while processing your request"

deal_participants_router = APIRouter()


async def get_participant_service(
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
    master_session: AsyncSession = Depends(get_master_async_session),
):
    service = DealParticipantService(
        session, user_ids.organisation_id, user_ids.user_id, master_session
    )
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("DealParticipantService close failed", exc_info=True)


def _map_participant_item(participant) -> DealParticipantItem:
    return DealParticipantItem(
        participant_id=participant.participant_id,
        deal_id=participant.deal_id,
        participant_type=str(participant.participant_type),
        related_local_id=participant.related_local_id,
        related_name=participant.related_name,
        related_email=participant.related_email,
        relationship_role=participant.relationship_role,
        is_deleted=participant.is_deleted,
    )


@deal_participants_router.post(
    "/deals/{deal_id}/participants",
    response_model=GenericResponse[DealParticipantsWriteResponse],
    status_code=201,
    tags=["crm", "participants"],
    summary="Add participants to a deal (internal CRM only)",
)
async def add_deal_participants(
    deal_id: UUID,
    request: DealParticipantsCreateRequest,
    response: FastAPIResponse,
    service: DealParticipantService = Depends(get_participant_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("deal_participants:create")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[DealParticipantsWriteResponse]:
    """Attach contacts, companies, or users to an internal CRM deal."""
    if not request.participants:
        return Response.error_response(
            message="No participants provided",
            status_code=400,
            response=response,
        )

    try:
        payloads = [item.model_dump() for item in request.participants]
        created, skipped, contact_changed, _ = await service.add_participants(deal_id, payloads)
        await service.session.commit()
        for participant in created:
            await service.session.refresh(participant)

        if contact_changed:
            try:
                from src.workers.deal_worker.client import send_deal_task_async
                from src.workers.deal_worker.services.deal_change_detector import (
                    StakeholderChangeType,
                )

                await send_deal_task_async(
                    "stakeholder.sync_deal_stakeholders",
                    kwargs={
                        "organisation_id": str(service.organisation_id),
                        "deal_id": str(deal_id),
                        "change_type": StakeholderChangeType.PARTICIPANT_ADDED.value,
                        "integration_id": None,
                        "integration_type": None,
                        "trigger_rematching": True,
                    },
                )
            except Exception:
                logger.warning(
                    "Failed to trigger stakeholder sync after participant addition",
                    exc_info=True,
                )

        payload = DealParticipantsWriteResponse(
            created=[_map_participant_item(p) for p in created],
            skipped=[
                DealParticipantSkippedItem(
                    participant_type=skip.participant_type,
                    related_id=skip.related_id,
                    relationship_role=skip.relationship_role,
                    reason=skip.reason,
                )
                for skip in skipped
            ],
            total_requested=len(request.participants),
            total_created=len(created),
            total_skipped=len(skipped),
        )

        return Response.created_response(
            message="Participants added successfully", data=payload, response=response
        )
    except DealParticipantServiceError as exc:
        await service.session.rollback()
        logger.warning("Participant add error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        await service.session.rollback()
        logger.error("Unexpected error adding participants: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@deal_participants_router.delete(
    "/deals/{deal_id}/participants/{participant_id}",
    response_model=GenericResponse[DealParticipantItem],
    tags=["crm", "participants"],
    summary="Remove a participant from a deal (internal CRM only)",
)
async def remove_deal_participant(
    deal_id: UUID,
    participant_id: UUID,
    response: FastAPIResponse,
    service: DealParticipantService = Depends(get_participant_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("deal_participants:delete")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[DealParticipantItem]:
    """Remove a participant from an internal CRM deal."""
    try:
        participant, contact_changed, _ = await service.remove_participant(deal_id, participant_id)
        await service.session.commit()
        await service.session.refresh(participant)

        if contact_changed:
            try:
                from src.workers.deal_worker.client import send_deal_task_async
                from src.workers.deal_worker.services.deal_change_detector import (
                    StakeholderChangeType,
                )

                await send_deal_task_async(
                    "stakeholder.sync_deal_stakeholders",
                    kwargs={
                        "organisation_id": str(service.organisation_id),
                        "deal_id": str(deal_id),
                        "change_type": StakeholderChangeType.PARTICIPANT_REMOVED.value,
                        "integration_id": None,
                        "integration_type": None,
                        "trigger_rematching": True,
                    },
                )
            except Exception:
                logger.warning(
                    "Failed to trigger stakeholder sync after participant removal",
                    exc_info=True,
                )

        payload = _map_participant_item(participant)
        return Response.success_response(message="Participant removed successfully", data=payload)
    except DealParticipantServiceError as exc:
        await service.session.rollback()
        logger.warning("Participant remove error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        await service.session.rollback()
        logger.error("Unexpected error removing participant: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@deal_participants_router.patch(
    "/deals/{deal_id}/participants/{participant_id}",
    response_model=GenericResponse[DealParticipantItem],
    tags=["crm", "participants"],
    summary="Update a participant role for a deal (internal CRM only)",
)
async def update_deal_participant(
    deal_id: UUID,
    participant_id: UUID,
    request: DealParticipantUpdateRequest,
    response: FastAPIResponse,
    service: DealParticipantService = Depends(get_participant_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("deal_participants:update")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[DealParticipantItem]:
    """Update role label for a deal participant."""
    try:
        participant = await service.update_participant_role(
            deal_id, participant_id, request.relationship_role
        )
        await service.session.commit()
        await service.session.refresh(participant)
        payload = _map_participant_item(participant)
        return Response.success_response(message="Participant updated successfully", data=payload)
    except DealParticipantServiceError as exc:
        await service.session.rollback()
        logger.warning("Participant update error: %s", exc.message, exc_info=True)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        await service.session.rollback()
        logger.error("Unexpected error updating participant: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )
