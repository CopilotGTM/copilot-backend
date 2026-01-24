"""Service for accessing CRM pipelines and stages."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync.crm.deals import CRMDealStage, CRMPipeline
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

    async def list_pipelines(self, integration_id: UUID | None = None) -> list[CRMPipeline]:
        try:
            stmt = select(CRMPipeline).where(
                col(CRMPipeline.organisation_id) == self.organisation_id,
                col(CRMPipeline.is_deleted).is_(False),
            )
            if integration_id:
                stmt = stmt.where(col(CRMPipeline.integration_id) == integration_id)

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
