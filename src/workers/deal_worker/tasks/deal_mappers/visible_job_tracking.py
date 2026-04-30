"""Helpers for deal-mapper tasks with optional user-visible job records."""

from __future__ import annotations

from uuid import UUID

from src.domain.models.organisation_tables.job_models import (
    DealJobs,
    DealJobType,
    JobStatus,
)
from src.infra.database.sessions import acquire_org_async_session
from src.workers.deal_worker.services.deal_job_publisher import DealJobRedisPublisher


class VisibleDealJobTracker:
    """Tracks a mapper task against a DealJobs row when a user-visible job_id exists."""

    def __init__(
        self,
        organisation_id: UUID,
        job_id: str | None,
        job_type: DealJobType,
    ):
        self.organisation_id = organisation_id
        self.job_id = UUID(job_id) if job_id else None
        self.job_type = job_type
        self.publisher: DealJobRedisPublisher | None = None

    async def start(self, stage: str, message: str, progress: float) -> None:
        """Mark the job active and publish queued plus first active progress frames."""
        if not self.job_id:
            return

        async for session in acquire_org_async_session(self.organisation_id):
            job = await session.get(DealJobs, self.job_id)
            if job:
                job.status = JobStatus.IN_PROGRESS
                await session.commit()
            break

        publisher = self._ensure_publisher()
        publisher.publish_progress("Queued", "queued", 0.0)
        publisher.publish_progress(message, stage, progress)

    async def complete(
        self,
        result_data: dict,
        saving_stage: str,
        saving_message: str,
    ) -> None:
        """Mark the job completed and publish saving, complete, and finalize frames."""
        if not self.job_id:
            return

        async for session in acquire_org_async_session(self.organisation_id):
            job = await session.get(DealJobs, self.job_id)
            if job:
                job.status = JobStatus.COMPLETED
                job.result_data = result_data
                await session.commit()
            break

        publisher = self._ensure_publisher()
        publisher.publish_progress(saving_message, saving_stage, 0.9)
        publisher.publish_progress("Complete", "complete", 1.0)
        publisher.publish_finalize("done")

    async def fail(self, message: str, error_code: str = "job_failed") -> None:
        """Mark the job failed and publish error/finalize frames."""
        if not self.job_id:
            return

        async for session in acquire_org_async_session(self.organisation_id):
            job = await session.get(DealJobs, self.job_id)
            if job:
                job.status = JobStatus.FAILED
                job.error_message = message[:500]
                job.retry_count += 1
                await session.commit()
            break

        publisher = self._ensure_publisher()
        publisher.publish_error(message, error_code)
        publisher.publish_finalize("error")

    def _ensure_publisher(self) -> DealJobRedisPublisher:
        if not self.job_id:
            raise RuntimeError("Cannot create a publisher without job_id")
        if not self.publisher:
            self.publisher = DealJobRedisPublisher(
                self.organisation_id,
                self.job_id,
                self.job_type.value,
            )
        return self.publisher
