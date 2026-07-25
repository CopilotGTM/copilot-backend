"""Post-commit batch dispatcher for contact communication hydration."""

from __future__ import annotations

from uuid import UUID

from src.domain.models.organisation_tables.job_models import DealJobType
from src.workers.deal_worker.client import send_deal_task_async

TASK_NAME = "deal.hydrate_contact_communications"
# copilotgtm: projection still performs one replay-safe upsert per source, so cap each
# transaction at 100 records; replace this ceiling with bulk SQL if profiling shows queue pressure.
CHANGED_SOURCE_CHUNK_SIZE = 100
CONTACT_CHUNK_SIZE = 100
DEAL_CHUNK_SIZE = 100


async def dispatch_contact_communication_hydration_async(
    *,
    organisation_id: UUID,
    integration_id: UUID | None = None,
    changed_email_ids: list[UUID | str] | None = None,
    changed_event_ids: list[UUID | str] | None = None,
    contact_ids: list[UUID | str] | None = None,
    deal_ids: list[UUID | str] | None = None,
    trigger: str,
    job_id: UUID | None = None,
    job_type: DealJobType | None = None,
    emit_events: bool = False,
) -> list[str]:
    """Enqueue bounded hydration, forwarding visibility only when explicitly requested."""
    if (job_id is None) != (job_type is None):
        raise ValueError("job_id and job_type must be supplied together")
    if not emit_events and job_id is not None:
        raise ValueError("job context requires emit_events=True")
    if emit_events and job_id is None:
        raise ValueError("emit_events=True requires job_id and job_type")

    email_ids = [str(value) for value in changed_email_ids or []]
    event_ids = [str(value) for value in changed_event_ids or []]
    explicit_contact_ids = [str(value) for value in contact_ids or []]
    explicit_deal_ids = [str(value) for value in deal_ids or []]
    payloads: list[dict[str, object]] = []

    source_ids_present = bool(email_ids or event_ids)
    if sum((bool(explicit_contact_ids), bool(explicit_deal_ids), source_ids_present)) > 1:
        raise ValueError("contact, deal, and changed-source hydration cannot be mixed")
    # Replay modes stay exclusive so no caller can send IDs that a worker silently ignores.
    if explicit_deal_ids:
        payloads.extend(
            {"deal_ids": explicit_deal_ids[offset : offset + DEAL_CHUNK_SIZE]}
            for offset in range(0, len(explicit_deal_ids), DEAL_CHUNK_SIZE)
        )
    elif explicit_contact_ids:
        payloads.extend(
            {"contact_ids": explicit_contact_ids[offset : offset + CONTACT_CHUNK_SIZE]}
            for offset in range(0, len(explicit_contact_ids), CONTACT_CHUNK_SIZE)
        )
    else:
        source_count = max(len(email_ids), len(event_ids))
        payloads.extend(
            {
                "changed_email_ids": email_ids[offset : offset + CHANGED_SOURCE_CHUNK_SIZE],
                "changed_event_ids": event_ids[offset : offset + CHANGED_SOURCE_CHUNK_SIZE],
            }
            for offset in range(0, source_count, CHANGED_SOURCE_CHUNK_SIZE)
        )

    # One client-visible job has one terminal state. Background callers may fan out,
    # but a requested SSE job must stay within one hydration task.
    if emit_events and len(payloads) != 1:
        raise ValueError("a visible job requires a single hydration batch")
    visibility = (
        {
            "job_id": str(job_id),
            "job_type": job_type.value,
            "emit_events": True,
        }
        if emit_events and job_id is not None and job_type is not None
        else {}
    )

    task_ids: list[str] = []
    # Each payload is independently replay-safe, so Celery can retry one failed chunk without
    # redoing successful chunks or requiring a parent orchestration task.
    for payload in payloads:
        task_id = await send_deal_task_async(
            TASK_NAME,
            kwargs={
                "organisation_id": str(organisation_id),
                "integration_id": str(integration_id) if integration_id else None,
                "trigger": trigger,
                **visibility,
                **payload,
            },
            queue="deal",
        )
        task_ids.append(str(task_id))
    return task_ids
