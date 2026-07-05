"""CRM pipelines API handler."""

from __future__ import annotations

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
from src.api.schemas.crm_pipeline_models import PipelineItem, PipelineStageItem
from src.api.schemas.crm_pipeline_write_models import (
    ClonePipelineRequest,
    CreatePipelineRequest,
    CreateStageRequest,
    MoveStageDealsRequest,
    MoveStageDealsResponse,
    ReorderStagesRequest,
    UpdatePipelineRequest,
    UpdateStageRequest,
)
from src.api.schemas.crm_settings_models import CRMSettingsLastChanged
from src.api.utils.http_response import Response
from src.infra.utils.logger import get_logger
from src.service.crm.access.pipeline_access_service import (
    PipelineAccessService,
    PipelineAccessServiceError,
)
from src.service.crm.deals.contracts import DealWriteServiceError
from src.service.crm.deals.write_service import DealWriteService
from src.service.crm.internal.internal_crm_setup_service import InternalCRMSetupService
from src.service.crm.internal.pipeline_write_service import (
    PipelineWriteService,
    PipelineWriteServiceError,
)
from src.service.crm.mutations.event_service import CRMMutationEventService

logger = get_logger(__name__)

_UNEXPECTED_ERR_MSG = "An unexpected error occurred while processing your request"

pipelines_router = APIRouter()


async def get_pipeline_access_service(
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    service = PipelineAccessService(session, user_ids.organisation_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("PipelineAccessService close failed", exc_info=True)


async def get_pipeline_write_service(
    user_ids: UserIDs = Depends(get_current_user),
    session: AsyncSession = Depends(get_org_async_session),
):
    service = PipelineWriteService(session, user_ids.organisation_id, user_ids.user_id)
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("PipelineWriteService close failed", exc_info=True)


async def get_deal_write_service(
    user_ids: UserIDs = Depends(get_current_user),
    org_session: AsyncSession = Depends(get_org_async_session),
    master_session: AsyncSession = Depends(get_master_async_session),
):
    service = DealWriteService(
        org_session=org_session,
        master_session=master_session,
        organisation_id=user_ids.organisation_id,
        actor_user_id=user_ids.user_id,
    )
    try:
        yield service
    finally:
        try:
            await service.close()
        except Exception:
            logger.debug("DealWriteService close failed", exc_info=True)


def _last_changed_from_event(event) -> CRMSettingsLastChanged | None:
    if event is None:
        return None
    return CRMSettingsLastChanged(
        changed_at=event.occurred_at,
        actor_user_id=event.actor_user_id,
        source=event.source,
        event_type=event.event_type,
    )


def _build_stage_item(
    stage,
    *,
    deal_count: int | None = None,
    last_changed_event=None,
) -> PipelineStageItem:
    return PipelineStageItem(
        stage_id=stage.stage_id,
        name=stage.name,
        label=stage.label,
        stage_state=str(stage.stage_state),
        probability=stage.probability,
        is_closed=stage.is_closed,
        display_order=stage.display_order,
        deal_count=deal_count,
        last_changed=_last_changed_from_event(last_changed_event),
    )


def _build_pipeline_item(
    pipeline,
    stages,
    *,
    deal_count: int | None = None,
    stage_counts: dict[UUID, int] | None = None,
    last_changed_event=None,
    stage_events: dict[UUID, object] | None = None,
) -> PipelineItem:
    stage_counts = stage_counts or {}
    stage_events = stage_events or {}
    return PipelineItem(
        pipeline_id=pipeline.pipeline_id,
        name=pipeline.name,
        label=pipeline.label,
        is_default=pipeline.is_default,
        is_active=pipeline.is_active,
        display_order=pipeline.display_order,
        updated_at=pipeline.updated_at,
        deal_count=deal_count,
        last_changed=_last_changed_from_event(last_changed_event),
        stages=[
            _build_stage_item(
                stage,
                deal_count=stage_counts.get(stage.stage_id, 0),
                last_changed_event=stage_events.get(stage.stage_id),
            )
            for stage in stages
        ],
    )


@pipelines_router.get(
    "/pipelines",
    response_model=GenericResponse[list[PipelineItem]],
    summary="List CRM pipelines (internal only)",
)
async def list_pipelines(
    response: FastAPIResponse,
    include_stages: bool = Query(False, description="Include stages in the response"),
    active_only: bool = Query(False, description="Return only pipelines usable for deal writes"),
    user_ids: UserIDs = Depends(get_current_user),
    service: PipelineAccessService = Depends(get_pipeline_access_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[list[PipelineItem]]:
    """List internal CRM pipelines for the organisation."""
    try:
        setup_service = InternalCRMSetupService(
            service.org_session, user_ids.organisation_id, user_ids.user_id
        )
        integration = await setup_service.get_internal_integration()
        if not integration:
            return Response.success_response(message="No internal pipelines", data=[])

        integration_id = integration.integration_id
        pipelines = await service.list_pipelines(
            integration_id=integration_id,
            active_only=active_only,
        )
        pipeline_ids = [pipeline.pipeline_id for pipeline in pipelines]
        pipeline_counts = await service.count_deals_by_pipeline(pipeline_ids)
        stages_by_pipeline = (
            await service.list_stages_by_pipeline(pipeline_ids) if include_stages else {}
        )
        stage_ids = [stage.stage_id for stages in stages_by_pipeline.values() for stage in stages]
        stage_counts = await service.count_deals_by_stage(stage_ids)
        audit_service = CRMMutationEventService(service.org_session, service.organisation_id)
        pipeline_events = await audit_service.latest_events_by_entity("pipeline", pipeline_ids)
        stage_events = await audit_service.latest_events_by_entity("stage", stage_ids)

        items: list[PipelineItem] = []
        for pipeline in pipelines:
            items.append(
                _build_pipeline_item(
                    pipeline,
                    stages_by_pipeline.get(pipeline.pipeline_id, []),
                    deal_count=pipeline_counts.get(pipeline.pipeline_id, 0),
                    stage_counts=stage_counts,
                    last_changed_event=pipeline_events.get(pipeline.pipeline_id),
                    stage_events=stage_events,
                )
            )

        return Response.success_response(message="Pipelines retrieved", data=items)
    except PipelineAccessServiceError as exc:
        logger.warning("Pipeline access error: %s", exc.message)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        logger.error("Unexpected error listing pipelines: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@pipelines_router.get(
    "/pipelines/{pipeline_id}/stages",
    response_model=GenericResponse[list[PipelineStageItem]],
    summary="List stages for a pipeline (internal only)",
)
async def list_pipeline_stages(
    pipeline_id: UUID,
    response: FastAPIResponse,
    service: PipelineAccessService = Depends(get_pipeline_access_service),
    _rl_read: None = Depends(rate_limit_user_read),
) -> GenericResponse[list[PipelineStageItem]]:
    """List stages for the specified pipeline."""
    try:
        stages = await service.list_stages(pipeline_id)
        stage_counts = await service.count_deals_by_stage([stage.stage_id for stage in stages])
        stage_events = await CRMMutationEventService(
            service.org_session,
            service.organisation_id,
        ).latest_events_by_entity("stage", [stage.stage_id for stage in stages])
        items = [
            _build_stage_item(
                stage,
                deal_count=stage_counts.get(stage.stage_id, 0),
                last_changed_event=stage_events.get(stage.stage_id),
            )
            for stage in stages
        ]
        return Response.success_response(message="Stages retrieved", data=items)
    except PipelineAccessServiceError as exc:
        logger.warning("Pipeline stage error: %s", exc.message)
        return Response.error_response(
            message=exc.message, status_code=exc.status_code, response=response
        )
    except Exception as exc:
        logger.error("Unexpected error listing stages: %s", exc, exc_info=True)
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


# ---------------------------------------------------------------------- writes


def _handle_write_error(
    exc: PipelineWriteServiceError, response: FastAPIResponse
) -> GenericResponse:
    logger.warning("Pipeline write error: %s", exc.message)
    return Response.error_response(
        message=exc.message, status_code=exc.status_code, response=response
    )


@pipelines_router.post(
    "/pipelines",
    response_model=GenericResponse[PipelineItem],
    summary="Create a new CRM pipeline",
)
async def create_pipeline(
    body: CreatePipelineRequest,
    response: FastAPIResponse,
    service: PipelineWriteService = Depends(get_pipeline_write_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("pipelines:create")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[PipelineItem]:
    """Create a new pipeline (optionally with stages) under the internal CRM."""
    try:
        pipeline, stages = await service.create_pipeline(body)
        return Response.success_response(
            message="Pipeline created", data=_build_pipeline_item(pipeline, stages)
        )
    except PipelineWriteServiceError as exc:
        return _handle_write_error(exc, response)
    except Exception:
        logger.exception("Unexpected error creating pipeline")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@pipelines_router.post(
    "/pipelines/{pipeline_id}/clone",
    response_model=GenericResponse[PipelineItem],
    summary="Clone a CRM pipeline",
)
async def clone_pipeline(
    pipeline_id: UUID,
    body: ClonePipelineRequest,
    response: FastAPIResponse,
    service: PipelineWriteService = Depends(get_pipeline_write_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("pipelines:create")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[PipelineItem]:
    """Create a new internal pipeline by copying another pipeline's stages."""
    try:
        pipeline, stages = await service.clone_pipeline(pipeline_id, body)
        return Response.success_response(
            message="Pipeline cloned", data=_build_pipeline_item(pipeline, stages)
        )
    except PipelineWriteServiceError as exc:
        return _handle_write_error(exc, response)
    except Exception:
        logger.exception("Unexpected error cloning pipeline")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@pipelines_router.patch(
    "/pipelines/{pipeline_id}",
    response_model=GenericResponse[PipelineItem],
    summary="Update a CRM pipeline",
)
async def update_pipeline(
    pipeline_id: UUID,
    body: UpdatePipelineRequest,
    response: FastAPIResponse,
    service: PipelineWriteService = Depends(get_pipeline_write_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("pipelines:update")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[PipelineItem]:
    """Rename a pipeline, flip the default flag, or change its display order."""
    try:
        pipeline = await service.update_pipeline(pipeline_id, body)
        return Response.success_response(
            message="Pipeline updated", data=_build_pipeline_item(pipeline, [])
        )
    except PipelineWriteServiceError as exc:
        return _handle_write_error(exc, response)
    except Exception:
        logger.exception("Unexpected error updating pipeline")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@pipelines_router.delete(
    "/pipelines/{pipeline_id}",
    response_model=GenericResponse[None],
    summary="Soft-delete a CRM pipeline",
)
async def delete_pipeline(
    pipeline_id: UUID,
    response: FastAPIResponse,
    service: PipelineWriteService = Depends(get_pipeline_write_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("pipelines:delete")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[None]:
    """Soft-delete a non-default pipeline that has no deals attached."""
    try:
        await service.delete_pipeline(pipeline_id)
        return Response.success_response(message="Pipeline deleted", data=None)
    except PipelineWriteServiceError as exc:
        return _handle_write_error(exc, response)
    except Exception:
        logger.exception("Unexpected error deleting pipeline")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@pipelines_router.post(
    "/pipelines/{pipeline_id}/stages",
    response_model=GenericResponse[PipelineStageItem],
    summary="Add a stage to a CRM pipeline",
)
async def create_pipeline_stage(
    pipeline_id: UUID,
    body: CreateStageRequest,
    response: FastAPIResponse,
    service: PipelineWriteService = Depends(get_pipeline_write_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("pipelines:create")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[PipelineStageItem]:
    """Append a new stage to an existing pipeline."""
    try:
        stage = await service.create_stage(pipeline_id, body)
        return Response.success_response(message="Stage created", data=_build_stage_item(stage))
    except PipelineWriteServiceError as exc:
        return _handle_write_error(exc, response)
    except Exception:
        logger.exception("Unexpected error creating stage")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@pipelines_router.patch(
    "/pipelines/{pipeline_id}/stages/reorder",
    response_model=GenericResponse[list[PipelineStageItem]],
    summary="Reorder stages in a CRM pipeline",
)
async def reorder_pipeline_stages(
    pipeline_id: UUID,
    body: ReorderStagesRequest,
    response: FastAPIResponse,
    service: PipelineWriteService = Depends(get_pipeline_write_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("pipelines:update")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[list[PipelineStageItem]]:
    """Save the full stage order atomically for one pipeline."""
    try:
        stages = await service.reorder_stages(pipeline_id, body)
        return Response.success_response(
            message="Stages reordered",
            data=[_build_stage_item(stage) for stage in stages],
        )
    except PipelineWriteServiceError as exc:
        return _handle_write_error(exc, response)
    except Exception:
        logger.exception("Unexpected error reordering stages")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@pipelines_router.patch(
    "/pipelines/{pipeline_id}/stages/{stage_id}",
    response_model=GenericResponse[PipelineStageItem],
    summary="Update a stage in a CRM pipeline",
)
async def update_pipeline_stage(
    pipeline_id: UUID,
    stage_id: UUID,
    body: UpdateStageRequest,
    response: FastAPIResponse,
    service: PipelineWriteService = Depends(get_pipeline_write_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("pipelines:update")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[PipelineStageItem]:
    """Rename a stage or update its probability/display order."""
    try:
        stage = await service.update_stage(pipeline_id, stage_id, body)
        return Response.success_response(message="Stage updated", data=_build_stage_item(stage))
    except PipelineWriteServiceError as exc:
        return _handle_write_error(exc, response)
    except Exception:
        logger.exception("Unexpected error updating stage")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@pipelines_router.post(
    "/pipelines/{pipeline_id}/stages/{stage_id}/move-deals",
    response_model=GenericResponse[MoveStageDealsResponse],
    summary="Move deals from one stage to another",
)
async def move_pipeline_stage_deals(
    pipeline_id: UUID,
    stage_id: UUID,
    body: MoveStageDealsRequest,
    response: FastAPIResponse,
    service: DealWriteService = Depends(get_deal_write_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("pipelines:update")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[MoveStageDealsResponse]:
    """Move all deals out of one stage before deleting or retiring it."""
    try:
        moved_count = await service.move_stage_deals(
            stage_id,
            body.destination_stage_id,
            pipeline_id=pipeline_id,
        )
        await service.session.commit()
        return Response.success_response(
            message="Stage deals moved",
            data=MoveStageDealsResponse(moved_deal_count=moved_count),
        )
    except DealWriteServiceError as exc:
        return Response.error_response(
            message=exc.message,
            status_code=exc.status_code,
            response=response,
        )
    except Exception:
        logger.exception("Unexpected error moving stage deals")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )


@pipelines_router.delete(
    "/pipelines/{pipeline_id}/stages/{stage_id}",
    response_model=GenericResponse[None],
    summary="Soft-delete a stage in a CRM pipeline",
)
async def delete_pipeline_stage(
    pipeline_id: UUID,
    stage_id: UUID,
    response: FastAPIResponse,
    service: PipelineWriteService = Depends(get_pipeline_write_service),
    _crm_write_guard: None = Depends(require_crm_write_scope("pipelines:delete")),
    _rl_write: None = Depends(rate_limit_user_write),
) -> GenericResponse[None]:
    """Soft-delete a stage that has no deals referencing it."""
    try:
        await service.delete_stage(pipeline_id, stage_id)
        return Response.success_response(message="Stage deleted", data=None)
    except PipelineWriteServiceError as exc:
        return _handle_write_error(exc, response)
    except Exception:
        logger.exception("Unexpected error deleting stage")
        return Response.error_response(
            message=_UNEXPECTED_ERR_MSG, status_code=500, response=response
        )
