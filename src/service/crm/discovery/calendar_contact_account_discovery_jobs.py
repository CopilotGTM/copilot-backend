from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import col
from src.domain.models.organisation_tables.job_models import DealJobs, DealJobType, JobStatus
from src.domain.models.organisation_tables.sync import IntegrationType, KnowledgeIntegration
from src.service.crm.internal.internal_crm_setup_service import AUTOMATED_PIPELINE_USER_ID

CALENDAR_DISCOVERY_METADATA_KEY = "calendar_contact_account_discovery_v1"
CALENDAR_DISCOVERY_RUN_VERSION = "calendar_contact_account_discovery_v1"
CALENDAR_DISCOVERY_DEDUPE_KEY_PREFIX = CALENDAR_DISCOVERY_RUN_VERSION
CALENDAR_DISCOVERY_TRIGGER_INITIAL_SYNC = "calendar_initial_sync"
CALENDAR_DISCOVERY_TRIGGER_CONNECTION = "calendar_connection"

_CALENDAR_INTEGRATION_TYPES = {
    IntegrationType.GOOGLE,
    IntegrationType.MICROSOFT,
}


@dataclass(frozen=True)
class CalendarDiscoveryJobDispatch:
    job_id: UUID
    task_kwargs: dict[str, str]


def calendar_discovery_should_enqueue(
    *,
    metadata: dict[str, Any] | None,
    is_initial: bool,
    records_synced: int,
    trigger: str = CALENDAR_DISCOVERY_TRIGGER_INITIAL_SYNC,
    trigger_id: str | None = None,
) -> bool:
    is_connection_trigger = trigger == CALENDAR_DISCOVERY_TRIGGER_CONNECTION
    if is_connection_trigger and not trigger_id:
        return False
    if not is_connection_trigger and (not is_initial or records_synced <= 0):
        return False

    state = dict((metadata or {}).get(CALENDAR_DISCOVERY_METADATA_KEY) or {})
    status = state.get("status")
    same_trigger = state.get("trigger") == trigger and (
        not is_connection_trigger or state.get("trigger_id") == trigger_id
    )
    if status == "success":
        return is_connection_trigger and not same_trigger
    if not is_connection_trigger and status in {"pending", "in_progress"} and state.get("job_id"):
        return False
    return not (status in {"pending", "in_progress"} and bool(state.get("job_id")) and same_trigger)


def should_run_calendar_discovery(
    *,
    integration: KnowledgeIntegration,
    is_initial: bool,
    records_synced: int,
    trigger: str = CALENDAR_DISCOVERY_TRIGGER_INITIAL_SYNC,
    trigger_id: str | None = None,
) -> bool:
    return calendar_discovery_should_enqueue(
        metadata=integration.connection_metadata,
        is_initial=is_initial,
        records_synced=records_synced,
        trigger=trigger,
        trigger_id=trigger_id,
    )


async def prepare_calendar_discovery_job(
    *,
    session: AsyncSession,
    organisation_id: UUID,
    source_integration_id: UUID,
    is_initial: bool,
    records_synced: int,
    trigger: str = CALENDAR_DISCOVERY_TRIGGER_INITIAL_SYNC,
    trigger_id: str | None = None,
) -> CalendarDiscoveryJobDispatch | None:
    is_connection_trigger = trigger == CALENDAR_DISCOVERY_TRIGGER_CONNECTION
    if is_connection_trigger and not trigger_id:
        return None
    if not is_connection_trigger and (not is_initial or records_synced <= 0):
        return None

    stmt = (
        select(KnowledgeIntegration)
        .where(
            col(KnowledgeIntegration.integration_id) == source_integration_id,
            col(KnowledgeIntegration.organisation_id) == organisation_id,
        )
        .with_for_update()
    )
    result = await session.execute(stmt)
    integration = result.scalars().first()
    if integration is None or not integration.is_active:
        return None

    integration_type = _normalize_integration_type(integration.integration_type)
    if integration_type not in _CALENDAR_INTEGRATION_TYPES:
        return None

    metadata = dict(integration.connection_metadata or {})
    if not calendar_discovery_should_enqueue(
        metadata=metadata,
        is_initial=is_initial,
        records_synced=records_synced,
        trigger=trigger,
        trigger_id=trigger_id,
    ):
        return None

    prior_state = dict(metadata.get(CALENDAR_DISCOVERY_METADATA_KEY) or {})
    previous_success_job_id = prior_state.get("last_success_job_id")
    previous_watermark = prior_state.get("last_processed_event_user_state_updated_at")
    now = datetime.now(UTC).isoformat()

    job = DealJobs(
        organisation_id=organisation_id,
        user_id=AUTOMATED_PIPELINE_USER_ID,
        job_type=DealJobType.CALENDAR_CONTACT_ACCOUNT_DISCOVERY,
        deal_id=None,
        event_id=None,
        stakeholder_id=None,
        status=JobStatus.PENDING,
        job_metadata={
            "source": "automated_pipeline",
            "trigger": trigger,
            "trigger_id": trigger_id,
            "run_version": CALENDAR_DISCOVERY_RUN_VERSION,
            "source_integration_id": str(source_integration_id),
            "integration_type": integration_type.value,
            "records_synced": records_synced,
            "dedupe_key": _build_dedupe_key(
                source_integration_id=source_integration_id,
                trigger=trigger,
                trigger_id=trigger_id,
            ),
            "previous_success_job_id": previous_success_job_id,
            "previous_event_user_state_updated_at": previous_watermark,
        },
    )
    session.add(job)
    await session.flush()

    metadata[CALENDAR_DISCOVERY_METADATA_KEY] = {
        **prior_state,
        "status": "pending",
        "job_id": str(job.job_id),
        "triggered_at": now,
        "trigger": trigger,
        "trigger_id": trigger_id,
        "last_success_job_id": previous_success_job_id,
        "last_processed_event_user_state_updated_at": previous_watermark,
        "records_synced": records_synced,
    }
    integration.connection_metadata = metadata
    flag_modified(integration, "connection_metadata")
    session.add(integration)
    await session.flush()

    return CalendarDiscoveryJobDispatch(
        job_id=job.job_id,
        task_kwargs={
            "organisation_id": str(organisation_id),
            "job_id": str(job.job_id),
        },
    )


async def mark_calendar_discovery_enqueue_failed(
    *,
    session: AsyncSession,
    organisation_id: UUID,
    job_id: UUID,
    error: str,
) -> None:
    job = await session.get(DealJobs, job_id)
    if job is None or job.organisation_id != organisation_id:
        return

    job.status = JobStatus.FAILED
    job.error_message = error[:500]
    metadata = dict(job.job_metadata or {})
    source_integration_id = _parse_uuid(metadata.get("source_integration_id"))

    if source_integration_id is not None:
        integration = await session.get(KnowledgeIntegration, source_integration_id)
        if integration is not None and integration.organisation_id == organisation_id:
            _write_discovery_metadata(
                integration,
                {
                    "status": "failed",
                    "job_id": str(job_id),
                    "failed_at": datetime.now(UTC).isoformat(),
                    "error": error[:500],
                },
            )

    session.add(job)


def build_discovery_result_payload(result: Any) -> dict[str, Any]:
    return {
        "events_considered": result.events_considered,
        "min_event_user_state_updated_at": _datetime_to_iso(result.min_event_user_state_updated_at),
        "max_event_user_state_updated_at": _datetime_to_iso(result.max_event_user_state_updated_at),
        "accounts_created": result.accounts_created,
        "contacts_created": result.contacts_created,
        "accounts_skipped": result.accounts_skipped,
        "contacts_skipped": result.contacts_skipped,
        "skip_counters": result.skip_counters,
    }


def _write_discovery_metadata(
    integration: KnowledgeIntegration,
    updates: dict[str, Any],
) -> None:
    metadata = dict(integration.connection_metadata or {})
    previous = dict(metadata.get(CALENDAR_DISCOVERY_METADATA_KEY) or {})
    metadata[CALENDAR_DISCOVERY_METADATA_KEY] = {**previous, **updates}
    integration.connection_metadata = metadata
    flag_modified(integration, "connection_metadata")


def _normalize_integration_type(value: IntegrationType | str) -> IntegrationType | None:
    if isinstance(value, IntegrationType):
        return value
    try:
        return IntegrationType(value)
    except ValueError:
        return None


def _parse_uuid(value: Any) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _build_dedupe_key(
    *,
    source_integration_id: UUID,
    trigger: str,
    trigger_id: str | None,
) -> str:
    key = f"{CALENDAR_DISCOVERY_DEDUPE_KEY_PREFIX}:{source_integration_id}"
    if trigger == CALENDAR_DISCOVERY_TRIGGER_CONNECTION and trigger_id:
        return f"{key}:{trigger}:{trigger_id}"
    return key


def _datetime_to_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
