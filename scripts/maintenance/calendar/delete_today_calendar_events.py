"""Delete all calendar events created today for a specific organisation.

Emergency cleanup script. Removes calendar events and all dependent records
(user_states, attendees, deal_associations, meetings, transcript_segments, deal_jobs)
created on or after a given date.

Dry run by default. Use --execute to persist.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlmodel import col

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

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
from src.infra.database.sessions import acquire_org_async_session  # noqa: E402
from src.infra.utils.logger import get_logger  # noqa: E402

logger = get_logger(__name__)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Delete calendar events created today for an org")
    parser.add_argument("--org-id", required=True, type=str, help="Organisation UUID")
    parser.add_argument("--execute", action="store_true", help="Persist deletions (default: dry run)")
    parser.add_argument(
        "--date",
        type=str,
        default=datetime.now(UTC).strftime("%Y-%m-%d"),
        help="Delete events created on or after this date (YYYY-MM-DD). Defaults to today.",
    )
    args = parser.parse_args()

    org_id = UUID(args.org_id)
    day_start = datetime.fromisoformat(f"{args.date}T00:00:00+00:00")
    day_end = datetime.fromisoformat(f"{args.date}T23:59:59+00:00")
    mode = "EXECUTE" if args.execute else "DRY RUN"

    print("=" * 90)
    print(f"  DELETE CALENDAR EVENTS CREATED ON {args.date} — {mode}")
    print(f"  Org: {org_id}")
    print("=" * 90)

    async for session in acquire_org_async_session(org_id):
        # Find events created today
        stmt = select(CalendarEvent).where(
            col(CalendarEvent.organisation_id) == org_id,
            col(CalendarEvent.created_at) >= day_start,
            col(CalendarEvent.created_at) <= day_end,
        )
        result = await session.execute(stmt)
        events = result.scalars().all()
        event_ids = [e.event_id for e in events]

        print(f"\n  Events found: {len(events)}")

        if not events:
            print("  Nothing to delete.")
            return

        # Show samples
        for e in events[:15]:
            print(f"    {e.event_id} title={e.title} created={e.created_at} external_id={e.external_id[:50]}...")
        if len(events) > 15:
            print(f"    ... {len(events) - 15} more")

        # Count child records
        def _count(model, col_name, ids):
            return select(func.count()).select_from(model).where(col(getattr(model, col_name)).in_(ids))

        us_count = (await session.execute(_count(CalendarEventUserState, "event_id", event_ids))).scalar() or 0
        att_count = (await session.execute(_count(CalendarAttendee, "event_id", event_ids))).scalar() or 0
        assoc_count = (await session.execute(_count(CalendarEventDealAssociation, "event_id", event_ids))).scalar() or 0
        dj_count = (await session.execute(_count(DealJobs, "event_id", event_ids))).scalar() or 0

        # Split meetings into today's vs old (pre-existing that got linked to today's events)
        mtg_stmt = select(Meeting).where(col(Meeting.calendar_event_id).in_(event_ids))
        mtg_result = await session.execute(mtg_stmt)
        all_meetings = mtg_result.scalars().all()

        new_meetings = [m for m in all_meetings if m.created_at and m.created_at >= day_start]
        old_meetings = [m for m in all_meetings if not m.created_at or m.created_at < day_start]
        new_meeting_ids = [m.meeting_id for m in new_meetings]
        old_meeting_ids = [m.meeting_id for m in old_meetings]

        ts_count_new = 0
        if new_meeting_ids:
            ts_count_new = (await session.execute(_count(TranscriptSegment, "meeting_id", new_meeting_ids))).scalar() or 0
        ts_count_old = 0
        if old_meeting_ids:
            ts_count_old = (await session.execute(_count(TranscriptSegment, "meeting_id", old_meeting_ids))).scalar() or 0

        print(f"\n  Child records:")
        print(f"    user_states: {us_count}")
        print(f"    attendees: {att_count}")
        print(f"    deal_associations: {assoc_count}")
        print(f"    deal_jobs: {dj_count}")
        print(f"    meetings (created today, WILL delete): {len(new_meetings)} ({ts_count_new} transcript segments)")
        print(f"    meetings (old, will UNLINK only):       {len(old_meetings)} ({ts_count_old} transcript segments)")

        if old_meetings:
            print(f"\n  OLD meetings that will be UNLINKED (not deleted):")
            for m in old_meetings:
                print(
                    f"    meeting_id={m.meeting_id} created={m.created_at} "
                    f"status={m.status} has_transcript={bool(m.full_transcript)}"
                )

        if not args.execute:
            print(f"\n  DRY RUN — no changes made. Run with --execute to delete.")
            return

        # Delete in FK-safe order
        # New meetings: delete transcript segments + meeting
        if new_meeting_ids:
            await session.execute(delete(TranscriptSegment).where(col(TranscriptSegment.meeting_id).in_(new_meeting_ids)))
            await session.execute(delete(Meeting).where(col(Meeting.meeting_id).in_(new_meeting_ids)))

        # Old meetings: unlink from calendar event (set calendar_event_id = NULL) so they survive
        for old_m in old_meetings:
            old_m.calendar_event_id = None
            session.add(old_m)

        await session.execute(delete(DealJobs).where(col(DealJobs.event_id).in_(event_ids)))
        await session.execute(delete(CalendarEventDealAssociation).where(col(CalendarEventDealAssociation.event_id).in_(event_ids)))
        await session.execute(delete(CalendarAttendee).where(col(CalendarAttendee.event_id).in_(event_ids)))
        await session.execute(delete(CalendarEventUserState).where(col(CalendarEventUserState.event_id).in_(event_ids)))
        await session.execute(delete(CalendarEvent).where(col(CalendarEvent.event_id).in_(event_ids)))

        await session.commit()
        print(f"\n  DELETED {len(events)} event(s) and all child records.")

    print("=" * 90)


if __name__ == "__main__":
    asyncio.run(main())
