"""Celery task for deal-update agent jobs."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from celery import Task

from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.workers.deal_worker.celery_app import app
from src.workers.deal_worker.services.deal_job_publisher import DealJobRedisPublisher
from src.workers.deal_worker.utils.deal_job_utils import get_organisation_name

logger = get_logger(__name__)


@app.task(
    bind=True,
    name="deal_update.process_job",
    acks_late=True,
    max_retries=3,
    soft_time_limit=600,
    time_limit=660,
)
def process_deal_update_job(self: Task, organisation_id: str, job_id: str):
    """Process a deal-update job through DealUpdateAgent."""

    async def _run():
        from src.agents.workflows.deal_update import DealUpdateAgent

        org_uuid = UUID(organisation_id)
        job_uuid = UUID(job_id)

        async for session in acquire_org_async_session(org_uuid):
            job = await session.get(DealJobs, job_uuid)
            if not job:
                raise RuntimeError(f"Deal update job {job_id} not found")
            if job.job_type != DealJobType.DEAL_UPDATE:
                raise RuntimeError(f"Invalid job type {job.job_type} for deal update task")
            if not job.deal_id:
                raise RuntimeError(f"Deal update job {job_id} has no deal_id")

            deal_id = job.deal_id
            metadata = job.job_metadata or {}
            job.status = JobStatus.IN_PROGRESS
            await session.commit()

        async for master_session in acquire_master_async_session():
            org_name = await get_organisation_name(master_session, org_uuid)

        publisher = DealJobRedisPublisher(org_uuid, job_uuid, DealJobType.DEAL_UPDATE.value)
        publisher.publish_progress("Running deal update agent...", "agent", 0.2)

        agent = DealUpdateAgent(organisation_id=org_uuid, deal_id=deal_id, org_name=org_name)
        result = await agent.generate(
            trigger_type=metadata.get("trigger_type") or "manual",
            trigger_source_id=metadata.get("trigger_source_id"),
            instruction=metadata.get("instruction"),
        )

        async for session in acquire_org_async_session(org_uuid):
            job = await session.get(DealJobs, job_uuid)
            if job:
                job.status = JobStatus.COMPLETED
                job.result_data = result.model_dump()
                if agent.last_token_usage:
                    job.token_usage = agent.last_token_usage.to_dict()
                    job.model_name = agent.last_token_usage.model_name
                    if agent.last_token_usage.cost is not None:
                        job.cost = Decimal(str(agent.last_token_usage.cost))
                    if agent.last_token_usage.duration_seconds is not None:
                        job.latency_ms = int(agent.last_token_usage.duration_seconds * 1000)
                await session.commit()

        result_payload = result.model_dump()
        publisher.publish_result(result_payload)
        publisher.publish_finalize("done")
        return {
            "status": "completed",
            "job_id": job_id,
            "job_type": DealJobType.DEAL_UPDATE.value,
        }

    try:
        return run_async(_run())
    except Exception as exc:
        logger.error(
            "Deal update task failed",
            extra={
                "event": "deal_update_task_failed",
                "organisation_id": organisation_id,
                "job_id": job_id,
                "failure_reason": str(exc),
            },
            exc_info=True,
        )
        raise self.retry(exc=exc, countdown=60 * (2**self.request.retries)) from exc
