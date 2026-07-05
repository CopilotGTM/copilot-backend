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
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.api.schemas.crm_pipeline_write_models import (
    ClonePipelineRequest,
    CreatePipelineRequest,
    CreateStageRequest,
    ReorderStagesRequest,
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
from src.service.crm.mutations.contracts import CRMMutationContext, CRMMutationRecordCommand
from src.service.crm.mutations.event_service import CRMMutationEventService
from src.service.crm.mutations.json_safe import changed_field
from src.service.crm.utils.pipeline_shape import (
    has_required_pipeline_shape,
    mark_pipeline_usability,
    required_pipeline_shape_message,
    stage_state_value,
)

logger = get_logger(__name__)

_SUPPORTED_STAGE_STATES = {state.value for state in CRMStageState}
_PIPELINE_AUDIT_FIELDS = ("name", "label", "is_default", "is_active", "display_order")
_STAGE_AUDIT_FIELDS = (
    "name",
    "label",
    "stage_state",
    "probability",
    "is_closed",
    "display_order",
)


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


def _normalise_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _same_timestamp(left: datetime, right: datetime) -> bool:
    return _normalise_timestamp(left) == _normalise_timestamp(right)


class PipelineWriteService:
    """Mutating operations for internal-CRM pipelines and stages."""

    def __init__(self, session: AsyncSession, organisation_id: UUID, user_id: UUID):
        self.session = session
        self.organisation_id = organisation_id
        self.user_id = user_id
        self._mutation_events = CRMMutationEventService(session, organisation_id)

    async def close(self) -> None:
        """Close the org-scoped session when this service owns cleanup."""
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

    async def _load_pipeline(
        self, pipeline_id: UUID, integration_id: UUID, *, for_update: bool = False
    ) -> CRMPipeline:
        stmt = select(CRMPipeline).where(
            col(CRMPipeline.pipeline_id) == pipeline_id,
            col(CRMPipeline.organisation_id) == self.organisation_id,
            col(CRMPipeline.integration_id) == integration_id,
            col(CRMPipeline.is_deleted).is_(False),
        )
        if for_update:
            stmt = stmt.with_for_update()
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

    async def _stage_name_or_key_exists(
        self,
        integration_id: UUID,
        pipeline_id: UUID,
        name: str,
        *,
        exclude_stage_id: UUID | None = None,
    ) -> bool:
        stage_key = _slugify(name)
        stmt = select(col(CRMDealStage.stage_id)).where(
            col(CRMDealStage.organisation_id) == self.organisation_id,
            col(CRMDealStage.integration_id) == integration_id,
            col(CRMDealStage.pipeline_id) == pipeline_id,
            col(CRMDealStage.is_deleted).is_(False),
            col(CRMDealStage.is_active).is_(True),
            or_(
                func.lower(col(CRMDealStage.name)) == name.lower(),
                col(CRMDealStage.stage_key) == stage_key,
            ),
        )
        if exclude_stage_id is not None:
            stmt = stmt.where(col(CRMDealStage.stage_id) != exclude_stage_id)
        result = await self.session.execute(stmt)
        return result.scalars().first() is not None

    def _ensure_unique_stage_inputs(self, stages: list[StageInput]) -> None:
        seen_names: set[str] = set()
        seen_keys: set[str] = set()
        for stage_def in stages:
            name_key = stage_def.name.lower()
            stage_key = _slugify(stage_def.name)
            if name_key in seen_names or stage_key in seen_keys:
                raise PipelineWriteServiceError(
                    f"A stage named '{stage_def.name}' already exists in this pipeline",
                    status_code=409,
                )
            seen_names.add(name_key)
            seen_keys.add(stage_key)

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
        """Create an internal-CRM pipeline and optional initial stages.

        The caller is mutating Copilot-owned CRM configuration, not provider
        configuration. A pipeline is only activated for deal writes when its
        stages can represent open, won, and lost deal lifecycle states.
        """
        integration_id = await self._get_internal_integration_id()

        if await self._pipeline_name_exists(integration_id, request.name):
            raise PipelineWriteServiceError(
                f"A pipeline named '{request.name}' already exists",
                status_code=409,
            )

        if request.stages:
            self._ensure_unique_stage_inputs(request.stages)

        # Default pipelines are used implicitly by deal creation, so they must
        # already contain the minimum lifecycle states at creation time.
        if request.set_as_default and not has_required_pipeline_shape(request.stages or []):
            raise PipelineWriteServiceError(required_pipeline_shape_message(), status_code=400)

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
            # A new pipeline becomes active only after its stages prove it can
            # carry a deal through open, won, and lost lifecycle actions.
            is_active=False,
            # New pipelines land at the end of the list.
            display_order=1,
            synced_at=now,
            last_seen_at=now,
            properties={"source": "internal_crm", "usable_for_internal_deals": False},
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

        # Store usability both as is_active and as explicit metadata.
        usable = mark_pipeline_usability(pipeline, created_stages)

        if request.set_as_default:
            if not usable:
                raise PipelineWriteServiceError(required_pipeline_shape_message(), status_code=400)
            await self._unset_existing_default(integration_id)
            pipeline.is_default = True
            await self.session.flush()

        await self._record_pipeline_event(
            pipeline.pipeline_id,
            operation="pipeline.create",
            event_type="crm.settings.pipeline.created",
            changed_fields=self._created_pipeline_changes(pipeline),
            summary=f"Created pipeline {pipeline.name}",
        )
        for stage in created_stages:
            await self._record_stage_event(
                stage.stage_id,
                operation="stage.create",
                event_type="crm.settings.stage.created",
                changed_fields=self._created_stage_changes(stage),
                summary=f"Created stage {stage.name}",
            )

        await self.session.commit()
        await self.session.refresh(pipeline)
        # Reload stages to capture any server-side defaults after flush.
        stages = await self._load_pipeline_stages(pipeline.pipeline_id)
        return pipeline, stages

    async def clone_pipeline(
        self, pipeline_id: UUID, request: ClonePipelineRequest
    ) -> tuple[CRMPipeline, list[CRMDealStage]]:
        """Create a pipeline by copying the source pipeline's stage definitions."""
        integration_id = await self._get_internal_integration_id()
        source_pipeline = await self._load_pipeline(pipeline_id, integration_id)
        source_stages = await self._load_pipeline_stages(source_pipeline.pipeline_id)
        cloned_stages: list[StageInput] = []
        for stage in source_stages:
            stage_state = stage_state_value(stage)
            if stage_state not in _SUPPORTED_STAGE_STATES:
                raise PipelineWriteServiceError(
                    "Source pipeline has an unsupported stage state",
                    status_code=400,
                )
            cloned_stages.append(
                StageInput(
                    name=stage.name,
                    label=stage.label,
                    stage_state=stage_state,
                    probability=stage.probability,
                    display_order=stage.display_order,
                )
            )
        return await self.create_pipeline(
            CreatePipelineRequest(
                name=request.name,
                label=request.label,
                set_as_default=request.set_as_default,
                stages=cloned_stages,
            )
        )

    async def update_pipeline(
        self, pipeline_id: UUID, request: UpdatePipelineRequest
    ) -> CRMPipeline:
        """Update one internal-CRM pipeline and propagate snapshot labels to deals.

        Promotion to default is guarded by the same lifecycle-shape rule used by
        deal creation, because default pipelines are selected implicitly by callers.
        """
        integration_id = await self._get_internal_integration_id()
        pipeline = await self._load_pipeline(pipeline_id, integration_id)
        if request.if_updated_at is None:
            raise PipelineWriteServiceError("if_updated_at is required", status_code=400)
        if not _same_timestamp(pipeline.updated_at, request.if_updated_at):
            raise PipelineWriteServiceError(
                "Pipeline changed since it was loaded. Refresh and retry.",
                status_code=409,
            )

        before = self._pipeline_snapshot(pipeline)

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
            stages = await self._load_pipeline_stages(pipeline.pipeline_id)
            # Promoting an incomplete pipeline would make default deal creation
            # fail later, so block it at the mutation boundary.
            if not pipeline.is_active or not has_required_pipeline_shape(stages):
                raise PipelineWriteServiceError(required_pipeline_shape_message(), status_code=400)
            await self._unset_existing_default(integration_id)
            pipeline.is_default = True

        pipeline.updated_at = utcnow()
        await self.session.flush()
        await self._record_pipeline_event(
            pipeline.pipeline_id,
            operation="pipeline.update",
            event_type="crm.settings.pipeline.updated",
            changed_fields=self._diff_snapshot(before, self._pipeline_snapshot(pipeline)),
            summary=f"Updated pipeline {pipeline.name}",
        )
        await self.session.commit()
        await self.session.refresh(pipeline)
        return pipeline

    async def delete_pipeline(self, pipeline_id: UUID) -> None:
        """Soft-delete an unused internal-CRM pipeline and its stages.

        Deletion is blocked for default pipelines and for pipelines referenced
        by live deals so existing deal rows never point at a deleted pipeline.
        """
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

        stages = await self._load_pipeline_stages(pipeline.pipeline_id)
        before = {"is_deleted": getattr(pipeline, "is_deleted", False)}
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
        await self._record_pipeline_event(
            pipeline.pipeline_id,
            operation="pipeline.delete",
            event_type="crm.settings.pipeline.deleted",
            changed_fields=self._diff_snapshot(before, {"is_deleted": pipeline.is_deleted}),
            summary=f"Deleted pipeline {pipeline.name}",
        )
        for stage in stages:
            before_stage = {"is_deleted": getattr(stage, "is_deleted", False)}
            stage.is_deleted = True
            await self._record_stage_event(
                stage.stage_id,
                operation="stage.delete",
                event_type="crm.settings.stage.deleted",
                changed_fields=self._diff_snapshot(
                    before_stage,
                    {"is_deleted": stage.is_deleted},
                ),
                summary=f"Deleted stage {stage.name}",
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
        """Create a stage inside an internal-CRM pipeline and refresh usability.

        Adding a stage may complete a draft pipeline's lifecycle shape, so the
        pipeline write gate is recomputed in the same transaction as the stage.
        """
        integration_id = await self._get_internal_integration_id()
        pipeline = await self._load_pipeline(pipeline_id, integration_id)

        if await self._stage_name_or_key_exists(
            integration_id,
            pipeline.pipeline_id,
            request.name,
        ):
            raise PipelineWriteServiceError(
                f"A stage named '{request.name}' already exists in this pipeline",
                status_code=409,
            )

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
        stages = await self._load_pipeline_stages(pipeline.pipeline_id)
        # Adding a stage can complete a previously unusable pipeline.
        mark_pipeline_usability(pipeline, stages)
        pipeline.updated_at = now
        await self.session.flush()
        await self._record_stage_event(
            stage.stage_id,
            operation="stage.create",
            event_type="crm.settings.stage.created",
            changed_fields=self._created_stage_changes(stage),
            summary=f"Created stage {stage.name}",
        )
        await self.session.commit()
        await self.session.refresh(stage)
        return stage

    async def reorder_stages(
        self, pipeline_id: UUID, request: ReorderStagesRequest
    ) -> list[CRMDealStage]:
        """Save the complete stage order in one transaction."""
        if len(set(request.stage_ids)) != len(request.stage_ids):
            raise PipelineWriteServiceError("Duplicate stage ids are not allowed", status_code=400)

        integration_id = await self._get_internal_integration_id()
        pipeline = await self._load_pipeline(pipeline_id, integration_id, for_update=True)
        if not _same_timestamp(pipeline.updated_at, request.if_updated_at):
            raise PipelineWriteServiceError(
                "Pipeline changed since it was loaded. Refresh and retry.",
                status_code=409,
            )

        stages = await self._load_pipeline_stages(pipeline.pipeline_id)
        stages_by_id = {stage.stage_id: stage for stage in stages}
        requested_ids = set(request.stage_ids)
        existing_ids = set(stages_by_id)
        if requested_ids != existing_ids:
            raise PipelineWriteServiceError(
                "Stage reorder must include every non-deleted stage in this pipeline.",
                status_code=409,
            )

        now = utcnow()
        for display_order, stage_id in enumerate(request.stage_ids, start=1):
            stage = stages_by_id[stage_id]
            before_order = stage.display_order
            stage.display_order = display_order
            if change := changed_field(before_order, display_order):
                await self._record_stage_event(
                    stage.stage_id,
                    operation="stage.reorder",
                    event_type="crm.settings.stage.reordered",
                    changed_fields={"display_order": change},
                    summary=f"Reordered stage {getattr(stage, 'name', stage.stage_id)}",
                )

        pipeline.updated_at = now
        await self.session.flush()
        await self.session.commit()
        stages = await self._load_pipeline_stages(pipeline_id)
        return sorted(stages, key=lambda stage: stage.display_order or 0)

    async def update_stage(
        self, pipeline_id: UUID, stage_id: UUID, request: UpdateStageRequest
    ) -> CRMDealStage:
        """Update one internal-CRM stage and propagate display snapshots to deals."""
        integration_id = await self._get_internal_integration_id()
        # Loading the pipeline enforces org ownership on the parent too.
        pipeline = await self._load_pipeline(pipeline_id, integration_id)
        stage = await self._load_stage(pipeline_id, stage_id, integration_id)
        if request.if_updated_at is None:
            raise PipelineWriteServiceError("if_updated_at is required", status_code=400)
        if not _same_timestamp(stage.updated_at, request.if_updated_at):
            raise PipelineWriteServiceError(
                "Stage changed since it was loaded. Refresh and retry.",
                status_code=409,
            )

        before = self._stage_snapshot(stage)

        if request.name is not None and request.name != stage.name:
            if await self._stage_name_or_key_exists(
                integration_id,
                pipeline_id,
                request.name,
                exclude_stage_id=stage.stage_id,
            ):
                raise PipelineWriteServiceError(
                    f"A stage named '{request.name}' already exists in this pipeline",
                    status_code=409,
                )
            new_label = request.label if request.label is not None else request.name
            stage.name = request.name
            stage.label = new_label
            # stage_key is a stable identifier; renames only change display snapshots.
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

        now = utcnow()
        stage.updated_at = now
        pipeline.updated_at = now

        await self.session.flush()
        await self._record_stage_event(
            stage.stage_id,
            operation="stage.update",
            event_type="crm.settings.stage.updated",
            changed_fields=self._diff_snapshot(before, self._stage_snapshot(stage)),
            summary=f"Updated stage {stage.name}",
        )
        await self.session.commit()
        await self.session.refresh(stage)
        return stage

    async def delete_stage(self, pipeline_id: UUID, stage_id: UUID) -> None:
        """Soft-delete an unused internal-CRM stage without breaking live pipelines.

        Usable pipelines cannot lose their final open, won, or lost stage,
        because deal lifecycle actions still need one target stage per state.
        """
        integration_id = await self._get_internal_integration_id()
        pipeline = await self._load_pipeline(pipeline_id, integration_id)
        stage = await self._load_stage(pipeline_id, stage_id, integration_id)

        deal_count = await self._count_deals_for_stage(stage.stage_id)
        if deal_count > 0:
            raise PipelineWriteServiceError(
                f"Cannot delete stage: {deal_count} deal(s) are currently in this stage. "
                "Move or delete those deals first.",
                status_code=409,
            )

        stages = await self._load_pipeline_stages(pipeline_id)
        remaining_stages = [existing for existing in stages if existing.stage_id != stage.stage_id]
        currently_usable = getattr(pipeline, "is_active", True) and has_required_pipeline_shape(
            stages
        )
        if currently_usable and not has_required_pipeline_shape(remaining_stages):
            # Do not let a usable pipeline lose its final lifecycle state. Deals
            # already on this pipeline still need valid close/reopen targets.
            state = stage_state_value(stage) or "required"
            raise PipelineWriteServiceError(
                f"Cannot delete the last {state} stage from a usable internal CRM pipeline.",
                status_code=409,
            )

        before = {"is_deleted": getattr(stage, "is_deleted", False)}
        stage.is_deleted = True
        # Unusable draft/import pipelines are allowed to stay incomplete, but
        # their write gate still mirrors the remaining stage shape after delete.
        mark_pipeline_usability(pipeline, remaining_stages)
        pipeline.updated_at = utcnow()
        await self.session.flush()
        await self._record_stage_event(
            stage.stage_id,
            operation="stage.delete",
            event_type="crm.settings.stage.deleted",
            changed_fields=self._diff_snapshot(before, {"is_deleted": stage.is_deleted}),
            summary=f"Deleted stage {stage.name}",
        )
        await self.session.commit()

    # ------------------------------------------------------------------ audit

    async def _record_pipeline_event(
        self,
        entity_id: UUID,
        *,
        operation: str,
        event_type: str,
        changed_fields: dict[str, dict[str, object]],
        summary: str,
    ) -> None:
        await self._record_settings_event(
            entity_type="pipeline",
            entity_id=entity_id,
            operation=operation,
            event_type=event_type,
            changed_fields=changed_fields,
            summary=summary,
        )

    async def _record_stage_event(
        self,
        entity_id: UUID,
        *,
        operation: str,
        event_type: str,
        changed_fields: dict[str, dict[str, object]],
        summary: str,
    ) -> None:
        await self._record_settings_event(
            entity_type="stage",
            entity_id=entity_id,
            operation=operation,
            event_type=event_type,
            changed_fields=changed_fields,
            summary=summary,
        )

    async def _record_settings_event(
        self,
        *,
        entity_type: str,
        entity_id: UUID,
        operation: str,
        event_type: str,
        changed_fields: dict[str, dict[str, object]],
        summary: str,
    ) -> None:
        await self._mutation_events.record_event(
            CRMMutationRecordCommand(
                entity_type=entity_type,
                entity_id=entity_id,
                event_type=event_type,
                operation=operation,
                changed_fields=changed_fields,
                semantic_event_types=[event_type],
                context=CRMMutationContext(source="api", actor_user_id=self.user_id),
                summary=summary,
            )
        )

    @staticmethod
    def _created_pipeline_changes(pipeline: CRMPipeline) -> dict[str, dict[str, object]]:
        return {
            name: {"old": None, "new": getattr(pipeline, name)} for name in _PIPELINE_AUDIT_FIELDS
        }

    @staticmethod
    def _created_stage_changes(stage: CRMDealStage) -> dict[str, dict[str, object]]:
        return {name: {"old": None, "new": getattr(stage, name)} for name in _STAGE_AUDIT_FIELDS}

    @staticmethod
    def _pipeline_snapshot(pipeline: CRMPipeline) -> dict[str, object]:
        return {name: getattr(pipeline, name, None) for name in _PIPELINE_AUDIT_FIELDS}

    @staticmethod
    def _stage_snapshot(stage: CRMDealStage) -> dict[str, object]:
        return {name: getattr(stage, name, None) for name in _STAGE_AUDIT_FIELDS}

    @staticmethod
    def _diff_snapshot(
        before: dict[str, object],
        after: dict[str, object],
    ) -> dict[str, dict[str, object]]:
        return {
            name: change
            for name, old in before.items()
            if (change := changed_field(old, after.get(name))) is not None
        }
