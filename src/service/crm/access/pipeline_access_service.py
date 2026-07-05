"""Service for accessing CRM pipelines and stages."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal, CRMDealStage, CRMPipeline
from src.infra.utils.logger import get_logger

logger = get_logger(__name__)


class PipelineAccessServiceError(Exception):
    """Custom exception for pipeline access errors."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class PipelineAccessService:
    """Service for listing pipelines and stages."""

    def __init__(self, session: AsyncSession, organisation_id: UUID):
        self.session = session
        self.organisation_id = organisation_id

    @property
    def org_session(self) -> AsyncSession:
        return self.session

    async def close(self) -> None:
        try:
            await self.session.close()
        except Exception:
            logger.debug("PipelineAccessService close exception", exc_info=True)

    async def list_pipelines(
        self, integration_id: UUID | None = None, *, active_only: bool = False
    ) -> list[CRMPipeline]:
        try:
            stmt = select(CRMPipeline).where(
                col(CRMPipeline.organisation_id) == self.organisation_id,
                col(CRMPipeline.is_deleted).is_(False),
            )
            if integration_id:
                stmt = stmt.where(col(CRMPipeline.integration_id) == integration_id)
            if active_only:
                stmt = stmt.where(col(CRMPipeline.is_active).is_(True))

            stmt = stmt.order_by(col(CRMPipeline.display_order).asc(), col(CRMPipeline.name))
            result = await self.session.execute(stmt)
            return list(result.scalars().all())
        except Exception as exc:
            logger.error("Failed to list pipelines: %s", exc, exc_info=True)
            raise PipelineAccessServiceError("Failed to list pipelines") from exc

    async def list_stages(self, pipeline_id: UUID) -> list[CRMDealStage]:
        try:
            stmt = select(CRMDealStage).where(
                col(CRMDealStage.organisation_id) == self.organisation_id,
                col(CRMDealStage.pipeline_id) == pipeline_id,
                col(CRMDealStage.is_deleted).is_(False),
            )
            stmt = stmt.order_by(col(CRMDealStage.display_order).asc(), col(CRMDealStage.name))
            result = await self.session.execute(stmt)
            return list(result.scalars().all())
        except Exception as exc:
            logger.error("Failed to list pipeline stages: %s", exc, exc_info=True)
            raise PipelineAccessServiceError("Failed to list pipeline stages") from exc

    async def list_stages_by_pipeline(
        self, pipeline_ids: list[UUID]
    ) -> dict[UUID, list[CRMDealStage]]:
        if not pipeline_ids:
            return {}
        try:
            stmt = (
                select(CRMDealStage)
                .where(
                    col(CRMDealStage.organisation_id) == self.organisation_id,
                    col(CRMDealStage.pipeline_id).in_(pipeline_ids),
                    col(CRMDealStage.is_deleted).is_(False),
                )
                .order_by(
                    col(CRMDealStage.pipeline_id),
                    col(CRMDealStage.display_order).asc(),
                    col(CRMDealStage.name),
                )
            )
            result = await self.session.execute(stmt)
            stages_by_pipeline = {pipeline_id: [] for pipeline_id in pipeline_ids}
            for stage in result.scalars().all():
                stages_by_pipeline.setdefault(stage.pipeline_id, []).append(stage)
            return stages_by_pipeline
        except Exception as exc:
            logger.error("Failed to list pipeline stages: %s", exc, exc_info=True)
            raise PipelineAccessServiceError("Failed to list pipeline stages") from exc

    async def count_deals_by_pipeline(self, pipeline_ids: list[UUID]) -> dict[UUID, int]:
        return await self._count_deals_by(CRMDeal.pipeline_id, pipeline_ids)

    async def count_deals_by_stage(self, stage_ids: list[UUID]) -> dict[UUID, int]:
        return await self._count_deals_by(CRMDeal.stage_id, stage_ids)

    async def _count_deals_by(self, column, ids: list[UUID]) -> dict[UUID, int]:
        if not ids:
            return {}
        stmt = (
            select(column, func.count(col(CRMDeal.deal_id)))
            .where(
                col(CRMDeal.organisation_id) == self.organisation_id,
                col(CRMDeal.is_deleted).is_(False),
                column.in_(ids),
            )
            .group_by(column)
        )
        result = await self.session.execute(stmt)
        return {row_id: int(count or 0) for row_id, count in result.all()}
