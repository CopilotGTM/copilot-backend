"""CRM pipelines API handler."""

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
from src.api.dependencies.rate_limit import rate_limit_user_read
from src.api.schemas.base_models import GenericResponse
from src.api.schemas.crm_pipeline_models import PipelineItem, PipelineStageItem
from src.api.utils.http_response import Response
from src.infra.utils.logger import get_logger
from src.service.crm.access.pipeline_access_service import (
    PipelineAccessService,
    PipelineAccessServiceError,
)
from src.service.crm.internal.internal_crm_setup_service import InternalCRMSetupService

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


def _build_pipeline_item(pipeline, stages) -> PipelineItem:
    return PipelineItem(
        pipeline_id=pipeline.pipeline_id,
        external_id=pipeline.external_id,
        name=pipeline.name,
        label=pipeline.label,
        is_default=pipeline.is_default,
        is_active=pipeline.is_active,
        display_order=pipeline.display_order,
        stages=[
            PipelineStageItem(
                stage_id=stage.stage_id,
                external_id=stage.external_id,
                name=stage.name,
                label=stage.label,
                stage_state=str(stage.stage_state),
                probability=stage.probability,
                is_closed=stage.is_closed,
                display_order=stage.display_order,
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
        pipelines = await service.list_pipelines(integration_id=integration_id)

        items: list[PipelineItem] = []
        for pipeline in pipelines:
            stages = []
            if include_stages:
                stages = await service.list_stages(pipeline.pipeline_id)
            items.append(_build_pipeline_item(pipeline, stages))

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
        items = [
            PipelineStageItem(
                stage_id=stage.stage_id,
                external_id=stage.external_id,
                name=stage.name,
                label=stage.label,
                stage_state=str(stage.stage_state),
                probability=stage.probability,
                is_closed=stage.is_closed,
                display_order=stage.display_order,
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
