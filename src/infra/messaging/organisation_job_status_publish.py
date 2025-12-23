"""Helpers to publish organisation job status updates to Redis.

Channel naming:
  org:job:{job_id}  -> per-job status updates for organisation operations

Payload (JSON):
  {
    "job_id": str,
    "status": str,
    "progress": int,
    "status_message": str | null,
    "result_data": dict | null,
    "error_message": str | null,
    "error_code": str | null,
    "updated_at": iso8601
  }

Lightweight (only core fields). SSE layer can enrich on snapshot if needed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from src.infra.messaging.publishers import RedisPublisher
from src.infra.messaging.redis_channels import channel_for_org_job
from src.infra.utils.logger import get_logger

logger = get_logger("org_job_publish")
_publisher = RedisPublisher(logger)


def publish_org_job_status(
    job_id: UUID | str,
    status: str,
    progress: int = 0,
    status_message: str | None = None,
    result_data: dict[str, Any] | None = None,
    error_message: str | None = None,
    error_code: str | None = None,
):
    """Publish organisation job status update to Redis.

    Called from Celery worker tasks to broadcast progress updates.
    SSE endpoints subscribe to the channel for real-time streaming.
    """
    payload = {
        "job_id": str(job_id),
        "status": status,
        "progress": progress,
        "status_message": status_message,
        "result_data": result_data,
        "error_message": error_message,
        "error_code": error_code,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    channel = channel_for_org_job(job_id)
    _publisher.publish(channel, payload, context_id=str(job_id))
