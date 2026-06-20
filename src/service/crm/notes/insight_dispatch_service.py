"""Post-commit deal insight dispatch for newly created notes."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.utils.pipeline_dispatch import (
    dispatch_deal_insight_pipeline,
    get_or_create_deal_memory_job,
)

logger = get_logger("notes_insight_dispatch_service")


class NotesInsightDispatchService:
    """Create a deal-memory job and dispatch the deal insight pipeline."""

    def __init__(self, org_session: AsyncSession, organisation_id: UUID):
        self.org_session = org_session
        self.organisation_id = organisation_id

    @property
    def session(self) -> AsyncSession:
        return self.org_session

    async def trigger_deal_insight_pipeline(
        self,
        deal_id: UUID,
        user_id: UUID,
        trigger_source: str = "note_creation",
        trigger_source_id: UUID | None = None,
    ) -> UUID | None:
        """Best-effort post-commit dispatch; note creation stays successful on failure."""
        try:
            job_id, _ = await get_or_create_deal_memory_job(
                self.org_session,
                self.organisation_id,
                deal_id,
                trigger_source,
                "new_note",
                user_id=user_id,
                trigger_source_id=trigger_source_id,
            )
            await self.org_session.commit()

            dispatch_deal_insight_pipeline(str(self.organisation_id), str(deal_id), str(job_id))
            logger.info(
                "Triggered deal insight pipeline after %s: deal=%s job=%s",
                trigger_source,
                deal_id,
                job_id,
            )
            return job_id
        except Exception as exc:
            logger.warning(
                "Failed to trigger deal memory pipeline: deal=%s error=%s",
                deal_id,
                str(exc),
            )
            return None
