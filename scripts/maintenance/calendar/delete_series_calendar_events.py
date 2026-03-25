"""Delete recurring/series calendar events and their dependent records.

Why this exists:
- We want a clean re-fetch of recurring calendar data after changing how
  canonical identity is derived for series/occurrence events.
- Single-instance meetings should remain untouched.

Series event detection:
- Google recurring records: `recurringEventId` or `originalStartTime`
- Outlook recurring records: `seriesMasterId`, `originalStart`, or
  `type in {"seriesMaster", "occurrence", "exception"}`

Dry run is the default. Use `--execute` to persist deletions.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlmodel import col

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.domain.models.master_tables import Organisation, OrganisationStatus  # noqa: E402
from src.domain.models.organisation_tables.job_models import DealJobs  # noqa: E402
from src.domain.models.organisation_tables.sync.calendar import (  # noqa: E402
    CalendarAttendee,
    CalendarEvent,
    CalendarEventDealAssociation,
    CalendarEventUserState,
)
from src.domain.models.organisation_tables.sync.meeting_intelligence import (  # noqa: E402
    Meeting,
    TranscriptSegment,
)
from src.infra.database.sessions import acquire_master_async_session, acquire_org_async_session  # noqa: E402
from src.infra.utils.logger import get_logger  # noqa: E402

logger = get_logger(__name__)


@dataclass
class RunStats:
    orgs_processed: int = 0
    events_matched: int = 0
    attendees_deleted: int = 0
    user_states_deleted: int = 0
    associations_deleted: int = 0
    meetings_deleted: int = 0
    transcript_segments_deleted: int = 0
    deal_jobs_deleted: int = 0
    events_deleted: int = 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Delete recurring/series calendar events and dependent records.",
    )
    parser.add_argument(
        "--org-id",
        action="append",
        help="Specific organisation UUID(s). Defaults to all approved organisations.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Persist deletions. Default is dry run.",
    )
    return parser.parse_args()


async def _get_target_org_ids(raw_ids: list[str] | None) -> list[UUID]:
    if raw_ids:
        return [UUID(value) for value in raw_ids]

    async for session in acquire_master_async_session():
        stmt = select(col(Organisation.organisation_id)).where(
            col(Organisation.status) == OrganisationStatus.APPROVED,
        )
        result = await session.execute(stmt)
        return [row[0] for row in result.all()]
    return []


def _is_series_event(event: CalendarEvent) -> bool:
    payload = event.data_payload if isinstance(event.data_payload, dict) else None
    if not payload:
        return False

    if payload.get("recurringEventId") or payload.get("originalStartTime"):
        return True

    event_type = payload.get("type")
    if event_type in {"seriesMaster", "occurrence", "exception"}:
        return True

    if payload.get("seriesMasterId") or payload.get("originalStart"):
        return True

    return False


async def _count_by_foreign_key(session, model, column_name: str, ids: list[UUID]) -> int:
    if not ids:
        return 0
    column = getattr(model, column_name)
    stmt = select(func.count()).select_from(model).where(col(column).in_(ids))
    result = await session.execute(stmt)
    return int(result.scalar() or 0)


async def _process_org(organisation_id: UUID, execute: bool, stats: RunStats) -> None:
    async for session in acquire_org_async_session(organisation_id):
        event_stmt = select(CalendarEvent).where(col(CalendarEvent.organisation_id) == organisation_id)
        event_result = await session.execute(event_stmt)
        all_events = event_result.scalars().all()
        series_events = [event for event in all_events if _is_series_event(event)]
        event_ids = [event.event_id for event in series_events]

        stats.orgs_processed += 1
        stats.events_matched += len(series_events)

        meeting_stmt = select(Meeting.meeting_id).where(col(Meeting.calendar_event_id).in_(event_ids))
        meeting_result = await session.execute(meeting_stmt)
        meeting_ids = [row[0] for row in meeting_result.all()]

        attendee_count = await _count_by_foreign_key(session, CalendarAttendee, "event_id", event_ids)
        user_state_count = await _count_by_foreign_key(session, CalendarEventUserState, "event_id", event_ids)
        association_count = await _count_by_foreign_key(
            session, CalendarEventDealAssociation, "event_id", event_ids
        )
        meeting_count = len(meeting_ids)
        transcript_count = await _count_by_foreign_key(
            session, TranscriptSegment, "meeting_id", meeting_ids
        )
        deal_job_count = await _count_by_foreign_key(session, DealJobs, "event_id", event_ids)

        print(f"\n--- Org: {organisation_id} ---")
        print(f"  series events matched: {len(series_events)}")
        print(f"  attendees to delete: {attendee_count}")
        print(f"  user_states to delete: {user_state_count}")
        print(f"  associations to delete: {association_count}")
        print(f"  meetings to delete: {meeting_count}")
        print(f"  transcript segments to delete: {transcript_count}")
        print(f"  deal_jobs to delete: {deal_job_count}")

        for event in series_events[:10]:
            payload = event.data_payload if isinstance(event.data_payload, dict) else {}
            print(
                "  sample: "
                f"event_id={event.event_id} provider={event.provider or '<null>'} "
                f"external_id={event.external_id} type={payload.get('type') or '<none>'} "
                f"recurringEventId={payload.get('recurringEventId') or '<none>'} "
                f"seriesMasterId={payload.get('seriesMasterId') or '<none>'}"
            )

        if len(series_events) > 10:
            print(f"  ... {len(series_events) - 10} more event(s)")

        if not execute or not event_ids:
            continue

        if meeting_ids:
            await session.execute(
                delete(TranscriptSegment).where(col(TranscriptSegment.meeting_id).in_(meeting_ids))
            )
        if event_ids:
            await session.execute(delete(DealJobs).where(col(DealJobs.event_id).in_(event_ids)))
            await session.execute(
                delete(CalendarEventDealAssociation).where(col(CalendarEventDealAssociation.event_id).in_(event_ids))
            )
            await session.execute(
                delete(CalendarAttendee).where(col(CalendarAttendee.event_id).in_(event_ids))
            )
            await session.execute(
                delete(CalendarEventUserState).where(col(CalendarEventUserState.event_id).in_(event_ids))
            )
            await session.execute(delete(Meeting).where(col(Meeting.calendar_event_id).in_(event_ids)))
            await session.execute(delete(CalendarEvent).where(col(CalendarEvent.event_id).in_(event_ids)))

        await session.commit()

        stats.attendees_deleted += attendee_count
        stats.user_states_deleted += user_state_count
        stats.associations_deleted += association_count
        stats.meetings_deleted += meeting_count
        stats.transcript_segments_deleted += transcript_count
        stats.deal_jobs_deleted += deal_job_count
        stats.events_deleted += len(event_ids)

        print(f"  deleted series events: {len(event_ids)}")


async def _main() -> None:
    args = _parse_args()
    org_ids = await _get_target_org_ids(args.org_id)
    stats = RunStats()

    mode = "EXECUTE" if args.execute else "DRY RUN"
    print("=" * 90)
    print(f"  DELETE SERIES CALENDAR EVENTS - {mode}")
    print("=" * 90)
    print(f"Processing {len(org_ids)} organisation(s)")

    for org_id in org_ids:
        await _process_org(org_id, args.execute, stats)

    print("\n" + "=" * 90)
    print(
        "TOTAL: "
        f"{stats.orgs_processed} org(s), "
        f"{stats.events_matched} series event(s) matched, "
        f"{stats.events_deleted} event(s) deleted, "
        f"{stats.attendees_deleted} attendee row(s) deleted, "
        f"{stats.user_states_deleted} user_state row(s) deleted, "
        f"{stats.associations_deleted} association row(s) deleted, "
        f"{stats.meetings_deleted} meeting row(s) deleted, "
        f"{stats.transcript_segments_deleted} transcript segment(s) deleted, "
        f"{stats.deal_jobs_deleted} deal_job row(s) deleted"
    )
    if not args.execute:
        print("DRY RUN - no changes made. Run with --execute to apply.")
    print("=" * 90)


if __name__ == "__main__":
    asyncio.run(_main())
