"""Organization-scoped CRM options used by trigger catalog and validation."""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.api.schemas.automation_trigger_models import (
    TriggerCatalogPipelineOption,
    TriggerCatalogStageOption,
)
from src.domain.models.organisation_tables import CRMDealStage, CRMPipeline


class AutomationTriggerOptionAccess:
    """Read only non-deleted CRM metadata inside one organization."""

    def __init__(self, session: AsyncSession, organisation_id: UUID) -> None:
        self.session = session
        self.organisation_id = organisation_id

    async def list_pipelines(self) -> list[TriggerCatalogPipelineOption]:
        """Return pipelines and stages for trigger-builder option rendering."""

        pipeline_columns = cast(Any, CRMPipeline).__table__.c
        stage_columns = cast(Any, CRMDealStage).__table__.c
        pipelines = list(
            (
                await self.session.execute(
                    select(CRMPipeline)
                    .where(
                        pipeline_columns.organisation_id == self.organisation_id,
                        pipeline_columns.is_deleted.is_(False),
                    )
                    .order_by(pipeline_columns.display_order, pipeline_columns.name)
                )
            )
            .scalars()
            .all()
        )
        stages = list(
            (
                await self.session.execute(
                    select(CRMDealStage)
                    .where(
                        stage_columns.organisation_id == self.organisation_id,
                        stage_columns.is_deleted.is_(False),
                    )
                    .order_by(
                        stage_columns.pipeline_id,
                        stage_columns.display_order,
                        stage_columns.name,
                    )
                )
            )
            .scalars()
            .all()
        )
        stages_by_pipeline: dict[UUID, list[TriggerCatalogStageOption]] = {}
        for stage in stages:
            stages_by_pipeline.setdefault(stage.pipeline_id, []).append(
                TriggerCatalogStageOption(
                    stage_id=stage.stage_id,
                    label=stage.label or stage.name,
                    stage_state=getattr(stage.stage_state, "value", stage.stage_state),
                    is_active=stage.is_active,
                )
            )
        return [
            TriggerCatalogPipelineOption(
                pipeline_id=pipeline.pipeline_id,
                label=pipeline.label or pipeline.name,
                is_active=pipeline.is_active,
                stages=stages_by_pipeline.get(pipeline.pipeline_id, []),
            )
            for pipeline in pipelines
        ]

    async def validate_crm_selection(self, pipeline_ids: set[UUID], stage_ids: set[UUID]) -> None:
        """Reject foreign, deleted, or cross-pipeline CRM identifiers."""

        pipeline_columns = cast(Any, CRMPipeline).__table__.c
        stage_columns = cast(Any, CRMDealStage).__table__.c
        if pipeline_ids:
            found_pipelines = set(
                (
                    await self.session.execute(
                        select(pipeline_columns.pipeline_id).where(
                            pipeline_columns.organisation_id == self.organisation_id,
                            pipeline_columns.is_deleted.is_(False),
                            pipeline_columns.pipeline_id.in_(pipeline_ids),
                        )
                    )
                ).scalars()
            )
            if found_pipelines != pipeline_ids:
                raise ValueError("Unknown CRM pipeline")
        if stage_ids:
            rows = (
                await self.session.execute(
                    select(stage_columns.stage_id, stage_columns.pipeline_id).where(
                        stage_columns.organisation_id == self.organisation_id,
                        stage_columns.is_deleted.is_(False),
                        stage_columns.stage_id.in_(stage_ids),
                    )
                )
            ).all()
            if {row.stage_id for row in rows} != stage_ids:
                raise ValueError("Unknown CRM stage")
            if pipeline_ids and any(row.pipeline_id not in pipeline_ids for row in rows):
                raise ValueError("CRM stages must belong to the selected pipeline")
