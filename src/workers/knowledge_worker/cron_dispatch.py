from __future__ import annotations

import argparse

from src.infra.utils.logger import configure_logging, get_logger
from src.workers.knowledge_worker.client import send_knowledge_task
from src.workers.knowledge_worker.scheduled_jobs import ScheduledJob, get_scheduled_job
from src.workers.knowledge_worker.scheduler_heartbeat import (
    record_scheduler_dispatch,
    record_scheduler_failure,
)

configure_logging(component="knowledge_cronjob")
logger = get_logger(__name__)


def dispatch_scheduled_job(job_name: str) -> str:
    """Enqueue the configured Tier 1 knowledge task for a CronJob trigger."""

    job: ScheduledJob = get_scheduled_job(job_name)

    try:
        task_id = send_knowledge_task(
            task_name=job.task_name,
            kwargs=dict(job.kwargs),
            queue=job.queue,
            routing_key=job.routing_key,
        )
        record_scheduler_dispatch(
            job_name=job.name,
            task_name=job.task_name,
            task_id=task_id,
            queue=job.queue,
            kwargs=dict(job.kwargs),
        )
        logger.info(
            "Triggered scheduled knowledge task",
            extra={
                "job_name": job.name,
                "task_name": job.task_name,
                "task_id": task_id,
                "queue": job.queue,
            },
        )
        return task_id
    except Exception as exc:
        record_scheduler_failure(job_name=job.name, task_name=job.task_name, error=str(exc))
        logger.exception(
            "Failed to trigger scheduled knowledge task",
            extra={"job_name": job.name, "task_name": job.task_name},
        )
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Dispatch a fixed knowledge schedule")
    parser.add_argument("--job", required=True, help="Scheduled job name")
    args = parser.parse_args()

    dispatch_scheduled_job(args.job)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
