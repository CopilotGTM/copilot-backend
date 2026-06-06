"""Write service for internal-CRM pipelines and stages.

Owns the invariants for create/update/delete of pipelines and their stages:

* Writes are scoped to the **internal** CRM integration for the current org
  (externally-synced pipelines like HubSpot are never touched).
* Renaming a pipeline or stage propagates the new name/label to the snapshot
  columns on ``crm_deals`` in the same transaction, because deal list views
  read those snapshots rather than joining back to the stage/pipeline row.
* Deletion is always soft (``is_deleted=True``) and is blocked when any
  non-deleted deal still references the row. The default pipeline can never
  be deleted — callers must promote another pipeline first.
"""

from __future__ import annotations

import re
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.api.schemas.crm_pipeline_write_models import (
    CreatePipelineRequest,
    CreateStageRequest,
    StageInput,
    UpdatePipelineRequest,
    UpdateStageRequest,
)
from src.domain.models.organisation_tables.base_mixins import utcnow
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealStage,
    CRMPipeline,
    CRMStageState,
)
from src.infra.utils.logger import get_logger
from src.service.crm.internal.internal_crm_setup_service import InternalCRMSetupService

logger = get_logger(__name__)


class PipelineWriteServiceError(Exception):
    """Raised when a pipeline/stage write fails a business-rule check."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _default_probability(state: CRMStageState, provided: float | None) -> float | None:
    if provided is not None:
        return provided
    if state == CRMStageState.WON:
        return 100.0
    if state == CRMStageState.LOST:
        return 0.0
    return None


class PipelineWriteService:
    """Mutating operations for internal-CRM pipelines and stages."""

    def __init__(self, session: AsyncSession, organisation_id: UUID, user_id: UUID):
        self.session = session
        self.organisation_id = organisation_id
        self.user_id = user_id

    async def close(self) -> None:
        try:
            await self.session.close()
        except Exception:
            logger.debug("PipelineWriteService close exception", exc_info=True)

    # ------------------------------------------------------------------ internal

    async def _get_internal_integration_id(self) -> UUID:
        setup = InternalCRMSetupService(self.session, self.organisation_id, self.user_id)
        integration = await setup.get_internal_integration()
        if not integration:
            raise PipelineWriteServiceError(
                "Internal CRM integration is not provisioned for this organisation",
                status_code=404,
            )
        return integration.integration_id

    async def _load_pipeline(self, pipeline_id: UUID, integration_id: UUID) -> CRMPipeline:
        stmt = select(CRMPipeline).where(
            col(CRMPipeline.pipeline_id) == pipeline_id,
            col(CRMPipeline.organisation_id) == self.organisation_id,
            col(CRMPipeline.integration_id) == integration_id,
            col(CRMPipeline.is_deleted).is_(False),
        )
        result = await self.session.execute(stmt)
        pipeline = result.scalars().first()
        if not pipeline:
            raise PipelineWriteServiceError("Pipeline not found", status_code=404)
        return pipeline

    async def _load_stage(
        self, pipeline_id: UUID, stage_id: UUID, integration_id: UUID
    ) -> CRMDealStage:
        stmt = select(CRMDealStage).where(
            col(CRMDealStage.stage_id) == stage_id,
            col(CRMDealStage.pipeline_id) == pipeline_id,
            col(CRMDealStage.organisation_id) == self.organisation_id,
            col(CRMDealStage.integration_id) == integration_id,
            col(CRMDealStage.is_deleted).is_(False),
        )
        result = await self.session.execute(stmt)
        stage = result.scalars().first()
        if not stage:
            raise PipelineWriteServiceError("Stage not found", status_code=404)
        return stage

    async def _pipeline_name_exists(
        self, integration_id: UUID, name: str, *, exclude_pipeline_id: UUID | None = None
    ) -> bool:
        stmt = select(col(CRMPipeline.pipeline_id)).where(
            col(CRMPipeline.organisation_id) == self.organisation_id,
            col(CRMPipeline.integration_id) == integration_id,
            func.lower(col(CRMPipeline.name)) == name.lower(),
            col(CRMPipeline.is_deleted).is_(False),
        )
        if exclude_pipeline_id is not None:
            stmt = stmt.where(col(CRMPipeline.pipeline_id) != exclude_pipeline_id)
        result = await self.session.execute(stmt)
        return result.scalars().first() is not None

    async def _count_deals_for_pipeline(self, pipeline_id: UUID) -> int:
        stmt = select(func.count()).where(
            col(CRMDeal.organisation_id) == self.organisation_id,
            col(CRMDeal.pipeline_id) == pipeline_id,
            col(CRMDeal.is_deleted).is_(False),
        )
        result = await self.session.execute(stmt)
        return int(result.scalar() or 0)

    async def _count_deals_for_stage(self, stage_id: UUID) -> int:
        stmt = select(func.count()).where(
            col(CRMDeal.organisation_id) == self.organisation_id,
            col(CRMDeal.stage_id) == stage_id,
            col(CRMDeal.is_deleted).is_(False),
        )
        result = await self.session.execute(stmt)
        return int(result.scalar() or 0)

    async def _unset_existing_default(self, integration_id: UUID) -> None:
        stmt = (
            update(CRMPipeline)
            .where(
                col(CRMPipeline.organisation_id) == self.organisation_id,
                col(CRMPipeline.integration_id) == integration_id,
                col(CRMPipeline.is_default).is_(True),
                col(CRMPipeline.is_deleted).is_(False),
            )
            .values(is_default=False)
        )
        await self.session.execute(stmt)

    async def _load_pipeline_stages(self, pipeline_id: UUID) -> list[CRMDealStage]:
        stmt = (
            select(CRMDealStage)
            .where(
                col(CRMDealStage.organisation_id) == self.organisation_id,
                col(CRMDealStage.pipeline_id) == pipeline_id,
                col(CRMDealStage.is_deleted).is_(False),
            )
            .order_by(col(CRMDealStage.display_order).asc().nulls_last())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    # ------------------------------------------------------------------ pipelines

    async def create_pipeline(
        self, request: CreatePipelineRequest
    ) -> tuple[CRMPipeline, list[CRMDealStage]]:
        integration_id = await self._get_internal_integration_id()

        if await self._pipeline_name_exists(integration_id, request.name):
            raise PipelineWriteServiceError(
                f"A pipeline named '{request.name}' already exists",
                status_code=409,
            )

        now = utcnow()
        pipeline_id = uuid4()
        pipeline = CRMPipeline(
            pipeline_id=pipeline_id,
            organisation_id=self.organisation_id,
            integration_id=integration_id,
            name=request.name,
            label=request.label or request.name,
            object_type="deals",
            is_default=False,
            is_active=True,
            # New pipelines land at the end of the list.
            display_order=1,
            synced_at=now,
            last_seen_at=now,
            properties={"source": "internal_crm"},
        )
        self.session.add(pipeline)
        await self.session.flush()
        await self.session.refresh(pipeline)

        created_stages: list[CRMDealStage] = []
        if request.stages:
            for stage_def in request.stages:
                created_stages.append(
                    self._build_stage_row(pipeline, stage_def, integration_id, now)
                )
            await self.session.flush()

        if request.set_as_default:
            await self._unset_existing_default(integration_id)
            pipeline.is_default = True
            await self.session.flush()

        await self.session.commit()
        await self.session.refresh(pipeline)
        # Reload stages to capture any server-side defaults after flush.
        stages = await self._load_pipeline_stages(pipeline.pipeline_id)
        return pipeline, stages

    async def update_pipeline(
        self, pipeline_id: UUID, request: UpdatePipelineRequest
    ) -> CRMPipeline:
        integration_id = await self._get_internal_integration_id()
        pipeline = await self._load_pipeline(pipeline_id, integration_id)

        # Guard: cannot clear the default flag without promoting another pipeline.
        if request.is_default is False and pipeline.is_default:
            raise PipelineWriteServiceError(
                "Cannot unset the default pipeline directly. "
                "Promote another pipeline by setting is_default=true on it instead.",
                status_code=400,
            )

        if request.name is not None and request.name != pipeline.name:
            if await self._pipeline_name_exists(
                integration_id, request.name, exclude_pipeline_id=pipeline.pipeline_id
            ):
                raise PipelineWriteServiceError(
                    f"A pipeline named '{request.name}' already exists",
                    status_code=409,
                )
            new_label = request.label if request.label is not None else request.name
            pipeline.name = request.name
            pipeline.label = new_label
            # Snapshot propagation — deal list views read these denormalized fields.
            await self.session.execute(
                update(CRMDeal)
                .where(
                    col(CRMDeal.organisation_id) == self.organisation_id,
                    col(CRMDeal.pipeline_id) == pipeline.pipeline_id,
                    col(CRMDeal.is_deleted).is_(False),
                )
                .values(pipeline_name=request.name, pipeline_label=new_label)
            )
        elif request.label is not None and request.label != pipeline.label:
            pipeline.label = request.label
            await self.session.execute(
                update(CRMDeal)
                .where(
                    col(CRMDeal.organisation_id) == self.organisation_id,
                    col(CRMDeal.pipeline_id) == pipeline.pipeline_id,
                    col(CRMDeal.is_deleted).is_(False),
                )
                .values(pipeline_label=request.label)
            )

        if request.display_order is not None:
            pipeline.display_order = request.display_order

        if request.is_default is True and not pipeline.is_default:
            await self._unset_existing_default(integration_id)
            pipeline.is_default = True

        await self.session.flush()
        await self.session.commit()
        await self.session.refresh(pipeline)
        return pipeline

    async def delete_pipeline(self, pipeline_id: UUID) -> None:
        integration_id = await self._get_internal_integration_id()
        pipeline = await self._load_pipeline(pipeline_id, integration_id)

        if pipeline.is_default:
            raise PipelineWriteServiceError(
                "Cannot delete the default pipeline. Promote another pipeline first.",
                status_code=409,
            )

        deal_count = await self._count_deals_for_pipeline(pipeline.pipeline_id)
        if deal_count > 0:
            raise PipelineWriteServiceError(
                f"Cannot delete pipeline: {deal_count} deal(s) still reference it.",
                status_code=409,
            )

        pipeline.is_deleted = True
        # Cascade-soft-delete the stages belonging to this pipeline so a later
        # rename-and-recreate doesn't collide on stage_key/name.
        await self.session.execute(
            update(CRMDealStage)
            .where(
                col(CRMDealStage.organisation_id) == self.organisation_id,
                col(CRMDealStage.pipeline_id) == pipeline.pipeline_id,
                col(CRMDealStage.is_deleted).is_(False),
            )
            .values(is_deleted=True)
        )
        await self.session.flush()
        await self.session.commit()

    # ------------------------------------------------------------------ stages

    def _build_stage_row(
        self,
        pipeline: CRMPipeline,
        stage_def: StageInput,
        integration_id: UUID,
        now,
        *,
        display_order_override: int | None = None,
    ) -> CRMDealStage:
        state = CRMStageState(stage_def.stage_state)
        is_closed = state in (CRMStageState.WON, CRMStageState.LOST)
        probability = _default_probability(state, stage_def.probability)
        stage_id = uuid4()
        display_order = (
            display_order_override
            if display_order_override is not None
            else (stage_def.display_order if stage_def.display_order is not None else 0)
        )
        stage = CRMDealStage(
            stage_id=stage_id,
            organisation_id=self.organisation_id,
            integration_id=integration_id,
            name=stage_def.name,
            label=stage_def.label or stage_def.name,
            stage_key=_slugify(stage_def.name),
            stage_state=state,
            probability=probability,
            forecast_category=None,
            is_closed=is_closed,
            display_order=display_order,
            pipeline_id=pipeline.pipeline_id,
            synced_at=now,
            last_seen_at=now,
        )
        self.session.add(stage)
        return stage

    async def create_stage(self, pipeline_id: UUID, request: CreateStageRequest) -> CRMDealStage:
        integration_id = await self._get_internal_integration_id()
        pipeline = await self._load_pipeline(pipeline_id, integration_id)

        display_order = request.display_order
        if display_order is None:
            stmt = select(func.max(CRMDealStage.display_order)).where(
                col(CRMDealStage.pipeline_id) == pipeline.pipeline_id,
                col(CRMDealStage.is_deleted).is_(False),
            )
            result = await self.session.execute(stmt)
            current_max = result.scalar()
            display_order = int(current_max or 0) + 1

        now = utcnow()
        stage = self._build_stage_row(
            pipeline,
            request,
            integration_id,
            now,
            display_order_override=display_order,
        )
        await self.session.flush()
        await self.session.commit()
        await self.session.refresh(stage)
        return stage

    async def update_stage(
        self, pipeline_id: UUID, stage_id: UUID, request: UpdateStageRequest
    ) -> CRMDealStage:
        integration_id = await self._get_internal_integration_id()
        # Loading the pipeline enforces org ownership on the parent too.
        await self._load_pipeline(pipeline_id, integration_id)
        stage = await self._load_stage(pipeline_id, stage_id, integration_id)

        if request.name is not None and request.name != stage.name:
            new_label = request.label if request.label is not None else request.name
            stage.name = request.name
            stage.label = new_label
            # Keep stage_key aligned with the display name so filters by key still work.
            stage.stage_key = _slugify(request.name)
            # Snapshot propagation — deal list views read these denormalized fields.
            await self.session.execute(
                update(CRMDeal)
                .where(
                    col(CRMDeal.organisation_id) == self.organisation_id,
                    col(CRMDeal.stage_id) == stage.stage_id,
                    col(CRMDeal.is_deleted).is_(False),
                )
                .values(stage_name=request.name, stage_label=new_label)
            )
        elif request.label is not None and request.label != stage.label:
            stage.label = request.label
            await self.session.execute(
                update(CRMDeal)
                .where(
                    col(CRMDeal.organisation_id) == self.organisation_id,
                    col(CRMDeal.stage_id) == stage.stage_id,
                    col(CRMDeal.is_deleted).is_(False),
                )
                .values(stage_label=request.label)
            )

        if request.probability is not None:
            stage.probability = request.probability
        if request.display_order is not None:
            stage.display_order = request.display_order

        await self.session.flush()
        await self.session.commit()
        await self.session.refresh(stage)
        return stage

    async def delete_stage(self, pipeline_id: UUID, stage_id: UUID) -> None:
        integration_id = await self._get_internal_integration_id()
        await self._load_pipeline(pipeline_id, integration_id)
        stage = await self._load_stage(pipeline_id, stage_id, integration_id)

        deal_count = await self._count_deals_for_stage(stage.stage_id)
        if deal_count > 0:
            raise PipelineWriteServiceError(
                f"Cannot delete stage: {deal_count} deal(s) are currently in this stage. "
                "Move or delete those deals first.",
                status_code=409,
            )

        stage.is_deleted = True
        await self.session.flush()
        await self.session.commit()
