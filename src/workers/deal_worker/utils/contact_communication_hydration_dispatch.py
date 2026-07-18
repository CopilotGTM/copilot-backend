"""Post-commit batch dispatcher for contact communication hydration."""

from __future__ import annotations

from uuid import UUID

from src.workers.deal_worker.client import send_deal_task_async

TASK_NAME = "deal.hydrate_contact_communications"
# copilotgtm: projection still performs one replay-safe upsert per source, so cap each
# transaction at 100 records; replace this ceiling with bulk SQL if profiling shows queue pressure.
CHANGED_SOURCE_CHUNK_SIZE = 100
CONTACT_CHUNK_SIZE = 100


async def dispatch_contact_communication_hydration_async(
    *,
    organisation_id: UUID,
    integration_id: UUID | None = None,
    changed_email_ids: list[UUID | str] | None = None,
    changed_event_ids: list[UUID | str] | None = None,
    contact_ids: list[UUID | str] | None = None,
    trigger: str,
) -> list[str]:
    """Enqueue bounded source batches or contact-backfill batches after commit."""
    email_ids = [str(value) for value in changed_email_ids or []]
    event_ids = [str(value) for value in changed_event_ids or []]
    explicit_contact_ids = [str(value) for value in contact_ids or []]
    payloads: list[dict[str, object]] = []

    # Explicit-contact and changed-source modes are exclusive: mixing them would select backfill
    # semantics and silently ignore the changed communication IDs.
    if explicit_contact_ids:
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
                **payload,
            },
            queue="deal",
        )
        task_ids.append(str(task_id))
    return task_ids
