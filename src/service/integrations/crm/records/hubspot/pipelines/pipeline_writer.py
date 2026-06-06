"""Active-snapshot writer for CRMPipeline + CRMDealStage from HubSpot.

Three passes inside a single session, committed once:

  Pass A — pipelines: insert / update / reactivate; build provider id→pipeline_id map.
  Pass B — stages:    insert / update / reactivate, resolving pipeline_id from the map.
                      Also updates pipeline_id when a stage's parent changed.
  Pass C — deactivation:
            - any pipeline with this (org, integration, object_type='deals') not in
              the incoming set → is_active=false.
            - any stage with this (org, integration) not in the incoming stage set
              → is_active=false. This single query covers both in-place removal and
              cascade from a deactivated pipeline (a stage under a removed pipeline
              is, by definition, not in the incoming set).

Every statement filters by (organisation_id, integration_id), and pipelines also
filter by object_type='deals'. The writer refuses to mutate rows outside this scope.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm import CRMSourceObjectType
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealStage,
    CRMPipeline,
)
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.common.source_mapping import CRMSourceMappingHelper
from src.service.integrations.crm.records.hubspot.pipelines.pipeline_types import (
    NormalizedPipeline,
    NormalizedStage,
    PipelineWriteResult,
)

logger = get_logger(__name__)

_OBJECT_TYPE = "deals"


class HubSpotPipelineWriter:
    """Writes deal pipelines + stages for one HubSpot integration's snapshot."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        organisation_id: UUID,
        integration_id: UUID,
    ) -> None:
        self.session = session
        self.organisation_id = organisation_id
        self.integration_id = integration_id
        self.source_mappings = CRMSourceMappingHelper(
            session=session,
            organisation_id=organisation_id,
            integration_id=integration_id,
            provider="hubspot",
        )

    async def apply(
        self,
        *,
        pipelines: list[NormalizedPipeline],
    ) -> PipelineWriteResult:
        """Apply the pipeline + stage snapshot. Caller commits the session."""
        result = PipelineWriteResult()
        now = datetime.now(UTC)

        # ── Pass A: pipelines ──────────────────────────────────────────────
        existing_pipelines = await self._load_existing_pipelines()
        incoming_pipeline_keys: set[str] = set()
        pipeline_map: dict[str, UUID] = {}

        for pipeline in pipelines:
            incoming_pipeline_keys.add(pipeline.external_id)
            existing = existing_pipelines.get(pipeline.external_id)
            if existing is None:
                row = self._new_pipeline_row(pipeline, now=now)
                self.session.add(row)
                await self._upsert_pipeline_mapping(pipeline, row.pipeline_id, now=now)
                pipeline_map[pipeline.external_id] = row.pipeline_id
                result.pipelines_created += 1
            else:
                was_active = bool(existing.is_active)
                was_deleted = bool(existing.is_deleted)
                self._apply_pipeline_updates(existing, pipeline, now=now)
                await self._upsert_pipeline_mapping(pipeline, existing.pipeline_id, now=now)
                if was_deleted:
                    existing.is_deleted = False
                    existing.is_active = True
                    result.pipelines_reactivated += 1
                elif was_active:
                    result.pipelines_updated += 1
                else:
                    existing.is_active = True
                    result.pipelines_reactivated += 1
                pipeline_map[pipeline.external_id] = existing.pipeline_id

        # ── Pass B: stages ─────────────────────────────────────────────────
        existing_stages = await self._load_existing_stages()
        incoming_stage_keys: set[str] = set()

        for pipeline in pipelines:
            new_pipeline_id = pipeline_map[pipeline.external_id]
            for stage in pipeline.stages:
                incoming_stage_keys.add(stage.external_id)
                existing_stage = existing_stages.get(stage.external_id)
                if existing_stage is None:
                    stage_row = self._new_stage_row(
                        stage,
                        pipeline_id=new_pipeline_id,
                        now=now,
                    )
                    self.session.add(stage_row)
                    await self._upsert_stage_mapping(stage, stage_row.stage_id, now=now)
                    result.stages_created += 1
                else:
                    was_active = bool(existing_stage.is_active)
                    was_deleted = bool(existing_stage.is_deleted)
                    self._apply_stage_updates(
                        existing_stage,
                        stage,
                        pipeline_id=new_pipeline_id,
                        now=now,
                    )
                    await self._upsert_stage_mapping(stage, existing_stage.stage_id, now=now)
                    if was_deleted:
                        existing_stage.is_deleted = False
                        existing_stage.is_active = True
                        result.stages_reactivated += 1
                    elif was_active:
                        result.stages_updated += 1
                    else:
                        existing_stage.is_active = True
                        result.stages_reactivated += 1

        # ── Pass C: deactivation ───────────────────────────────────────────
        for external_id, existing in existing_pipelines.items():
            if existing.is_active and external_id not in incoming_pipeline_keys:
                existing.is_active = False
                result.pipelines_deactivated += 1
        await self.source_mappings.mark_missing_deleted(
            object_type=CRMSourceObjectType.PIPELINE,
            seen_external_ids=incoming_pipeline_keys,
        )

        for external_id, existing_stage in existing_stages.items():
            if existing_stage.is_active and external_id not in incoming_stage_keys:
                existing_stage.is_active = False
                result.stages_deactivated += 1
        await self.source_mappings.mark_missing_deleted(
            object_type=CRMSourceObjectType.STAGE,
            seen_external_ids=incoming_stage_keys,
        )

        await self.session.flush()
        logger.info(
            "Pipeline writer apply complete",
            extra={
                "organisation_id": str(self.organisation_id),
                "integration_id": str(self.integration_id),
                "pipelines_created": result.pipelines_created,
                "pipelines_updated": result.pipelines_updated,
                "pipelines_reactivated": result.pipelines_reactivated,
                "pipelines_deactivated": result.pipelines_deactivated,
                "stages_created": result.stages_created,
                "stages_updated": result.stages_updated,
                "stages_reactivated": result.stages_reactivated,
                "stages_deactivated": result.stages_deactivated,
            },
        )
        return result

    # ── Load helpers ──────────────────────────────────────────────────────

    async def _load_existing_pipelines(self) -> dict[str, CRMPipeline]:
        """Load every mapped pipeline for this integration's deal scope."""
        external_to_local = await self.source_mappings.load_local_id_map(
            object_type=CRMSourceObjectType.PIPELINE,
            include_deleted=True,
        )
        if not external_to_local:
            return {}
        stmt = select(CRMPipeline).where(
            col(CRMPipeline.organisation_id) == self.organisation_id,
            col(CRMPipeline.integration_id) == self.integration_id,
            col(CRMPipeline.object_type) == _OBJECT_TYPE,
            col(CRMPipeline.pipeline_id).in_(set(external_to_local.values())),
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        local_to_external = {
            local_id: external_id for external_id, local_id in external_to_local.items()
        }
        return {
            local_to_external[row.pipeline_id]: row
            for row in rows
            if row.pipeline_id in local_to_external
        }

    async def _load_existing_stages(self) -> dict[str, CRMDealStage]:
        """Load every mapped stage for this integration."""
        external_to_local = await self.source_mappings.load_local_id_map(
            object_type=CRMSourceObjectType.STAGE,
            include_deleted=True,
        )
        if not external_to_local:
            return {}
        stmt = select(CRMDealStage).where(
            col(CRMDealStage.organisation_id) == self.organisation_id,
            col(CRMDealStage.integration_id) == self.integration_id,
            col(CRMDealStage.stage_id).in_(set(external_to_local.values())),
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        local_to_external = {
            local_id: external_id for external_id, local_id in external_to_local.items()
        }
        return {
            local_to_external[row.stage_id]: row
            for row in rows
            if row.stage_id in local_to_external
        }

    async def _upsert_pipeline_mapping(
        self,
        pipeline: NormalizedPipeline,
        local_pipeline_id: UUID,
        *,
        now: datetime,
    ) -> None:
        await self.source_mappings.upsert_integration_mapping(
            external_object_type=CRMSourceObjectType.PIPELINE,
            external_id=pipeline.external_id,
            local_object_type=CRMSourceObjectType.PIPELINE,
            local_object_id=local_pipeline_id,
            data_payload=pipeline.data_payload,
            source_created_at=None,
            source_modified_at=pipeline.source_modified_at,
            synced_at=now,
        )

    async def _upsert_stage_mapping(
        self,
        stage: NormalizedStage,
        local_stage_id: UUID,
        *,
        now: datetime,
    ) -> None:
        await self.source_mappings.upsert_integration_mapping(
            external_object_type=CRMSourceObjectType.STAGE,
            external_id=stage.external_id,
            local_object_type=CRMSourceObjectType.STAGE,
            local_object_id=local_stage_id,
            data_payload=stage.data_payload,
            source_created_at=None,
            source_modified_at=stage.source_modified_at,
            synced_at=now,
        )

    # ── Row construction ─────────────────────────────────────────────────

    def _new_pipeline_row(self, pipeline: NormalizedPipeline, *, now: datetime) -> CRMPipeline:
        return CRMPipeline(
            organisation_id=self.organisation_id,
            integration_id=self.integration_id,
            name=pipeline.name,
            label=pipeline.label,
            object_type=pipeline.object_type,
            is_default=pipeline.is_default,
            is_active=True,
            display_order=pipeline.display_order,
            source_modified_at=pipeline.source_modified_at,
            synced_at=now,
            last_seen_at=now,
        )

    def _apply_pipeline_updates(
        self,
        row: CRMPipeline,
        pipeline: NormalizedPipeline,
        *,
        now: datetime,
    ) -> None:
        row.name = pipeline.name
        row.label = pipeline.label
        row.object_type = pipeline.object_type
        row.is_default = pipeline.is_default
        row.display_order = pipeline.display_order
        row.source_modified_at = pipeline.source_modified_at
        row.synced_at = now
        row.last_seen_at = now

    def _new_stage_row(
        self,
        stage: NormalizedStage,
        *,
        pipeline_id: UUID,
        now: datetime,
    ) -> CRMDealStage:
        return CRMDealStage(
            organisation_id=self.organisation_id,
            integration_id=self.integration_id,
            name=stage.name,
            label=stage.label,
            stage_key=stage.stage_key,
            stage_state=stage.stage_state,
            probability=stage.probability,
            is_closed=stage.is_closed,
            is_active=True,
            display_order=stage.display_order,
            pipeline_id=pipeline_id,
            source_modified_at=stage.source_modified_at,
            synced_at=now,
            last_seen_at=now,
        )

    def _apply_stage_updates(
        self,
        row: CRMDealStage,
        stage: NormalizedStage,
        *,
        pipeline_id: UUID,
        now: datetime,
    ) -> None:
        # pipeline_id is updated even on existing rows so a stage whose parent
        # pipeline changed in HubSpot gets its FK corrected.
        row.pipeline_id = pipeline_id
        row.name = stage.name
        row.label = stage.label
        row.stage_key = stage.stage_key
        row.stage_state = stage.stage_state
        row.probability = stage.probability
        row.is_closed = stage.is_closed
        row.display_order = stage.display_order
        row.source_modified_at = stage.source_modified_at
        row.synced_at = now
        row.last_seen_at = now
