from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from celery import Task

from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.infra.database.sessions import master_async_session_scope, org_async_session_scope
from src.infra.utils.async_runner import run_async
from src.infra.utils.logger import get_logger
from src.service.crm.discovery.calendar_contact_account_discovery import (
    CalendarContactAccountDiscoveryService,
)
from src.service.crm.discovery.calendar_contact_account_discovery_jobs import (
    CALENDAR_DISCOVERY_METADATA_KEY,
    _parse_uuid,
    _write_discovery_metadata,
    build_discovery_result_payload,
    prepare_calendar_discovery_job,
    should_run_calendar_discovery,
)
from src.workers.deal_worker.celery_app import app

logger = get_logger(__name__)

__all__ = [
    "CALENDAR_DISCOVERY_METADATA_KEY",
    "discover_calendar_contacts_accounts_task",
    "should_run_calendar_discovery",
]


@app.task(
    bind=True,
    name="deal.discover_calendar_contacts_accounts",
    acks_late=True,
    max_retries=2,
    default_retry_delay=60,
    soft_time_limit=600,
    time_limit=660,
)
def discover_calendar_contacts_accounts_task(
    self: Task,
    sync_result: dict[str, Any] | None = None,
    organisation_id: str | None = None,
    source_integration_id: str | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Derive internal CRM accounts/contacts from persisted calendar events."""
    started_at = datetime.now(UTC)
    if organisation_id is None:
        raise RuntimeError("Calendar discovery requires organisation_id")
    org_id = UUID(organisation_id)

    async def _run() -> dict[str, Any]:
        job_uuid = await _resolve_or_create_job(
            org_id=org_id,
            job_id=job_id,
            source_integration_id=source_integration_id,
            sync_result=sync_result,
        )
        if job_uuid is None:
            return {
                "status": "skipped",
                "reason": "sync_not_successful_or_empty",
                "organisation_id": organisation_id,
                "source_integration_id": source_integration_id,
                "started_at": started_at.isoformat(),
                "completed_at": datetime.now(UTC).isoformat(),
            }

        job_source_integration_id: UUID
        watermark: datetime | None

        async with org_async_session_scope(org_id) as org_session:
            job = await org_session.get(DealJobs, job_uuid)
            if job is None:
                raise RuntimeError(f"Calendar discovery job {job_id} not found")
            if job.organisation_id != org_id:
                raise RuntimeError(f"Calendar discovery job {job_id} belongs to another org")
            if job.job_type != DealJobType.CALENDAR_CONTACT_ACCOUNT_DISCOVERY:
                raise RuntimeError(f"Invalid job type {job.job_type} for calendar discovery task")

            metadata = dict(job.job_metadata or {})
            parsed_integration_id = _parse_uuid(metadata.get("source_integration_id"))
            if parsed_integration_id is None:
                raise RuntimeError("Calendar discovery job missing source_integration_id")

            job_source_integration_id = parsed_integration_id
            watermark = _parse_datetime(metadata.get("previous_event_user_state_updated_at"))
            job.status = JobStatus.IN_PROGRESS

            integration = await org_session.get(KnowledgeIntegration, job_source_integration_id)
            if integration is not None and integration.organisation_id == org_id:
                _write_discovery_metadata(
                    integration,
                    {
                        "status": "in_progress",
                        "job_id": str(job_uuid),
                        "task_id": self.request.id,
                        "started_at": started_at.isoformat(),
                    },
                )
                org_session.add(integration)

            org_session.add(job)
            await org_session.commit()

        try:
            async with (
                master_async_session_scope() as master_session,
                org_async_session_scope(org_id) as org_session,
            ):
                service = CalendarContactAccountDiscoveryService(
                    org_session=org_session,
                    master_session=master_session,
                    organisation_id=org_id,
                    source_integration_id=job_source_integration_id,
                    event_user_state_updated_after=watermark,
                )
                result = await service.run()
                await org_session.commit()
        except Exception as exc:
            await _record_failure(
                org_id=org_id,
                job_id=job_uuid,
                error=str(exc),
                final=self.request.retries >= self.max_retries,
            )
            raise

        result_payload = build_discovery_result_payload(result)
        completed_at = datetime.now(UTC)

        async with org_async_session_scope(org_id) as org_session:
            job = await org_session.get(DealJobs, job_uuid)
            if job is None:
                raise RuntimeError(f"Calendar discovery job {job_id} disappeared")

            job.status = JobStatus.COMPLETED
            job.result_data = result_payload
            job.error_message = None

            metadata = dict(job.job_metadata or {})
            integration = await org_session.get(KnowledgeIntegration, job_source_integration_id)
            if integration is not None and integration.organisation_id == org_id:
                _write_discovery_metadata(
                    integration,
                    {
                        "status": "success",
                        "job_id": str(job_uuid),
                        "task_id": self.request.id,
                        "completed_at": completed_at.isoformat(),
                        "last_success_job_id": str(job_uuid),
                        "last_processed_event_user_state_updated_at": result_payload[
                            "max_event_user_state_updated_at"
                        ],
                        "records_synced": metadata.get("records_synced"),
                        **result_payload,
                    },
                )
                org_session.add(integration)

            org_session.add(job)
            await org_session.commit()

        return {
            "status": "success",
            "organisation_id": organisation_id,
            "job_id": str(job_uuid),
            "source_integration_id": str(job_source_integration_id),
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            **result_payload,
        }

    try:
        return run_async(_run())
    except Exception as exc:
        logger.error(
            "Calendar contact/account discovery task failed",
            exc_info=True,
            extra={
                "organisation_id": organisation_id,
                "job_id": job_id,
                "source_integration_id": source_integration_id,
                "error": str(exc),
            },
        )
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=60 * (2**self.request.retries)) from exc
        return {
            "status": "failed",
            "organisation_id": organisation_id,
            "job_id": job_id,
            "source_integration_id": source_integration_id,
            "started_at": started_at.isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "error": str(exc),
            "error_type": type(exc).__name__,
        }


async def _resolve_or_create_job(
    *,
    org_id: UUID,
    job_id: str | None,
    source_integration_id: str | None,
    sync_result: dict[str, Any] | None,
) -> UUID | None:
    if job_id:
        return UUID(job_id)

    records_synced = int((sync_result or {}).get("events_synced") or 0)
    if (sync_result or {}).get("status") != "success" or records_synced <= 0:
        return None
    if source_integration_id is None:
        raise RuntimeError("Calendar discovery requires source_integration_id")

    integration_uuid = UUID(source_integration_id)
    async with org_async_session_scope(org_id) as org_session:
        dispatch = await prepare_calendar_discovery_job(
            session=org_session,
            organisation_id=org_id,
            source_integration_id=integration_uuid,
            is_initial=True,
            records_synced=records_synced,
        )
        await org_session.commit()
        return dispatch.job_id if dispatch is not None else None


async def _record_failure(
    *,
    org_id: UUID,
    job_id: UUID,
    error: str,
    final: bool,
) -> None:
    async with org_async_session_scope(org_id) as org_session:
        job = await org_session.get(DealJobs, job_id)
        if job is None:
            return

        job.retry_count += 1
        job.error_message = error[:500]
        job.status = JobStatus.FAILED if final else JobStatus.PENDING

        metadata = dict(job.job_metadata or {})
        source_integration_id = _parse_uuid(metadata.get("source_integration_id"))
        if source_integration_id is not None:
            integration = await org_session.get(KnowledgeIntegration, source_integration_id)
            if integration is not None and integration.organisation_id == org_id:
                _write_discovery_metadata(
                    integration,
                    {
                        "status": "failed" if final else "pending",
                        "job_id": str(job_id),
                        "failed_at": datetime.now(UTC).isoformat() if final else None,
                        "error": error[:500],
                    },
                )
                org_session.add(integration)

        org_session.add(job)
        await org_session.commit()


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
