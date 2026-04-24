"""Active-snapshot writer for Salesforce Opportunity pipelines + stages."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDealStage,
    CRMPipeline,
)
from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.salesforce.pipelines.pipeline_types import (
    NormalizedPipeline,
    NormalizedStage,
    PipelineWriteResult,
)

logger = get_logger(__name__)

_OBJECT_TYPE = "deals"


class SalesforcePipelineWriter:
    """Writes Opportunity RecordType pipelines + StageName stages for one integration."""

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

    async def apply(
        self,
        *,
        pipelines: list[NormalizedPipeline],
    ) -> PipelineWriteResult:
        result = PipelineWriteResult()
        now = datetime.now(UTC)

        existing_pipelines = await self._load_existing_pipelines()
        incoming_pipeline_keys: set[str] = set()
        pipeline_map: dict[str, UUID] = {}

        for pipeline in pipelines:
            incoming_pipeline_keys.add(pipeline.external_id)
            existing = existing_pipelines.get(pipeline.external_id)
            if existing is None:
                row = self._new_pipeline_row(pipeline, now=now)
                self.session.add(row)
                await self.session.flush()
                pipeline_map[pipeline.external_id] = row.pipeline_id
                result.pipelines_created += 1
            else:
                was_active = bool(existing.is_active)
                self._apply_pipeline_updates(existing, pipeline, now=now)
                if was_active:
                    result.pipelines_updated += 1
                else:
                    existing.is_active = True
                    result.pipelines_reactivated += 1
                pipeline_map[pipeline.external_id] = existing.pipeline_id

        existing_stages = await self._load_existing_stages()
        incoming_stage_keys: set[str] = set()

        for pipeline in pipelines:
            pipeline_id = pipeline_map[pipeline.external_id]
            for stage in pipeline.stages:
                incoming_stage_keys.add(stage.external_id)
                existing_stage = existing_stages.get(stage.external_id)
                if existing_stage is None:
                    self.session.add(self._new_stage_row(stage, pipeline_id=pipeline_id, now=now))
                    result.stages_created += 1
                else:
                    was_active = bool(existing_stage.is_active)
                    self._apply_stage_updates(
                        existing_stage, stage, pipeline_id=pipeline_id, now=now
                    )
                    if was_active:
                        result.stages_updated += 1
                    else:
                        existing_stage.is_active = True
                        result.stages_reactivated += 1

        for external_id, existing in existing_pipelines.items():
            if existing.is_active and external_id not in incoming_pipeline_keys:
                existing.is_active = False
                result.pipelines_deactivated += 1

        for external_id, existing_stage in existing_stages.items():
            if existing_stage.is_active and external_id not in incoming_stage_keys:
                existing_stage.is_active = False
                result.stages_deactivated += 1

        await self.session.flush()
        logger.info(
            "Salesforce pipeline writer apply complete",
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

    async def _load_existing_pipelines(self) -> dict[str, CRMPipeline]:
        stmt = select(CRMPipeline).where(
            col(CRMPipeline.organisation_id) == self.organisation_id,
            col(CRMPipeline.integration_id) == self.integration_id,
            col(CRMPipeline.object_type) == _OBJECT_TYPE,
            col(CRMPipeline.is_deleted) == False,  # noqa: E712
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return {row.external_id: row for row in rows if row.external_id is not None}

    async def _load_existing_stages(self) -> dict[str, CRMDealStage]:
        stmt = select(CRMDealStage).where(
            col(CRMDealStage.organisation_id) == self.organisation_id,
            col(CRMDealStage.integration_id) == self.integration_id,
            col(CRMDealStage.is_deleted) == False,  # noqa: E712
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return {row.external_id: row for row in rows if row.external_id is not None}

    def _new_pipeline_row(self, pipeline: NormalizedPipeline, *, now: datetime) -> CRMPipeline:
        return CRMPipeline(
            organisation_id=self.organisation_id,
            integration_id=self.integration_id,
            external_id=pipeline.external_id,
            name=pipeline.name,
            label=pipeline.label,
            object_type=pipeline.object_type,
            is_default=pipeline.is_default,
            is_active=True,
            display_order=pipeline.display_order,
            data_payload=pipeline.data_payload,
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
        row.data_payload = pipeline.data_payload
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
            external_id=stage.external_id,
            name=stage.name,
            label=stage.label,
            stage_key=stage.stage_key,
            stage_state=stage.stage_state,
            probability=stage.probability,
            forecast_category=stage.forecast_category,
            is_closed=stage.is_closed,
            is_active=True,
            display_order=stage.display_order,
            pipeline_id=pipeline_id,
            data_payload=stage.data_payload,
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
        row.pipeline_id = pipeline_id
        row.name = stage.name
        row.label = stage.label
        row.stage_key = stage.stage_key
        row.stage_state = stage.stage_state
        row.probability = stage.probability
        row.forecast_category = stage.forecast_category
        row.is_closed = stage.is_closed
        row.display_order = stage.display_order
        row.data_payload = stage.data_payload
        row.source_modified_at = stage.source_modified_at
        row.synced_at = now
        row.last_seen_at = now
