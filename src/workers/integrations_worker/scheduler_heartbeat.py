from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from src.infra.messaging.redis_client import get_redis_client
from src.infra.utils.logger import get_logger
from src.workers.integrations_worker.scheduled_jobs import SCHEDULED_JOBS, ScheduledJob

logger = get_logger(__name__)

_KEY_PREFIX = "integrations:scheduler"
_RETENTION_SECONDS = 60 * 60 * 24 * 14


def _write_scheduler_state(job_name: str, payload: dict[str, Any]) -> None:
    """Persist last dispatch metadata so ops can inspect scheduler freshness."""

    redis = get_redis_client()
    redis.set(f"{_KEY_PREFIX}:{job_name}", json.dumps(payload), ex=_RETENTION_SECONDS)


def _read_scheduler_state(job_name: str) -> dict[str, Any] | None:
    redis = get_redis_client()
    raw_payload = redis.get(f"{_KEY_PREFIX}:{job_name}")
    if raw_payload is None:
        return None

    if isinstance(raw_payload, bytes):
        raw_payload = raw_payload.decode("utf-8")

    payload = json.loads(raw_payload)
    if not isinstance(payload, dict):
        raise ValueError("Scheduler heartbeat payload must be a JSON object")
    return payload


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _build_missing_status(job: ScheduledJob) -> dict[str, Any]:
    return {
        "healthy": True,
        "status": "missing",
        "job_name": job.name,
        "task_name": job.task_name,
        "schedule": job.schedule,
        "max_lag_seconds": job.max_lag_seconds,
        "note": "No scheduler heartbeat recorded yet for this job",
    }


def _build_job_status(
    job: ScheduledJob, payload: dict[str, Any] | None, now: datetime
) -> dict[str, Any]:
    if payload is None:
        return _build_missing_status(job)

    failed_at = _parse_timestamp(payload.get("failed_at"))
    if payload.get("status") == "failed":
        return {
            "healthy": False,
            "status": "failed",
            "job_name": job.name,
            "task_name": job.task_name,
            "schedule": job.schedule,
            "max_lag_seconds": job.max_lag_seconds,
            "last_event_at": failed_at.isoformat() if failed_at else None,
            "error": payload.get("error"),
        }

    triggered_at = _parse_timestamp(payload.get("triggered_at"))
    if triggered_at is None:
        return _build_missing_status(job)

    age_seconds = max(0, int((now - triggered_at).total_seconds()))
    is_healthy = age_seconds <= job.max_lag_seconds
    return {
        "healthy": is_healthy,
        "status": "healthy" if is_healthy else "stale",
        "job_name": job.name,
        "task_name": job.task_name,
        "schedule": job.schedule,
        "max_lag_seconds": job.max_lag_seconds,
        "last_event_at": triggered_at.isoformat(),
        "age_seconds": age_seconds,
        "task_id": payload.get("task_id"),
        "queue": payload.get("queue"),
    }


def get_scheduler_health(*, now: datetime | None = None) -> dict[str, Any]:
    """Return scheduler freshness for all fixed integrations schedules."""

    current_time = now or datetime.now(UTC)
    jobs: dict[str, Any] = {}
    overall_healthy = True

    for job_name, job in SCHEDULED_JOBS.items():
        job_status = _build_job_status(job, _read_scheduler_state(job_name), current_time)
        jobs[job_name] = job_status
        overall_healthy = overall_healthy and bool(job_status["healthy"])

    return {
        "healthy": overall_healthy,
        "checked_at": current_time.isoformat(),
        "jobs": jobs,
    }


def record_scheduler_dispatch(
    *,
    job_name: str,
    task_name: str,
    task_id: str,
    queue: str,
    kwargs: dict[str, Any],
) -> None:
    """Record a successful enqueue of a fixed integrations schedule."""

    payload = {
        "status": "triggered",
        "job_name": job_name,
        "task_name": task_name,
        "task_id": task_id,
        "queue": queue,
        "kwargs": kwargs,
        "triggered_at": datetime.now(UTC).isoformat(),
    }
    try:
        _write_scheduler_state(job_name, payload)
    except Exception as exc:
        logger.warning(
            "Failed to persist scheduler dispatch heartbeat",
            extra={"job_name": job_name, "task_name": task_name, "error": str(exc)},
        )


def record_scheduler_failure(
    *,
    job_name: str,
    task_name: str,
    error: str,
) -> None:
    """Record a scheduler enqueue failure for operator debugging."""

    payload = {
        "status": "failed",
        "job_name": job_name,
        "task_name": task_name,
        "error": error,
        "failed_at": datetime.now(UTC).isoformat(),
    }
    try:
        _write_scheduler_state(job_name, payload)
    except Exception as exc:
        logger.warning(
            "Failed to persist scheduler failure heartbeat",
            extra={"job_name": job_name, "task_name": task_name, "error": str(exc)},
        )
