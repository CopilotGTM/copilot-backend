"""Deduplicate calendar events into one shared row per real meeting occurrence.

Root cause: calendar_events.external_id historically stored the mailbox-specific
provider event ID instead of the shared calendar UID. That created duplicate
CalendarEvent rows when multiple users in the same org synced the same meeting.

This script backfills the final identity model:
1. Derive provider, shared UID, occurrence_anchor, and original_start_at from data_payload
2. Group duplicates by (shared UID, occurrence_anchor)
3. Pick a winner, merge child rows, and delete losers
4. Update surviving events to the canonical shared identity fields

Tables affected:
- calendar_events (delete loser rows; backfill identity fields on survivors)
- calendar_event_deal_associations (delete loser's associations)
- calendar_event_user_states (re-point to winner so both integrations are tracked)
- calendar_event_attendees (delete loser's, winner keeps its own snapshot)
- meetings + transcript_segments (re-point to winner, or delete if winner already has one)
- deal_jobs (re-point event_id to winner)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import load_only
from sqlmodel import col

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.domain.models.organisation_tables.job_models import DealJobs  # noqa: E402
from src.domain.models.organisation_tables.sync.calendar import (  # noqa: E402
    CalendarAttendee,
    CalendarEvent,
    CalendarEventDealAssociation,
    CalendarEventUserState,
    GenerationStatus,
)
from src.domain.models.organisation_tables.sync.meeting_intelligence import (  # noqa: E402
    Meeting,
    MeetingStatus,
    TranscriptSegment,
)
from src.infra.database.sessions import (  # noqa: E402
    acquire_master_async_session,
    acquire_org_async_session,
)
from src.infra.utils.logger import get_logger  # noqa: E402

logger = get_logger(__name__)
ISO_UTC_SUFFIX = "+00:00"

# Only columns that exist in the pre-migration schema. Avoids
# "column does not exist" errors when migration 037 hasn't run yet.
_SAFE_LOAD = load_only(
    CalendarEvent.event_id,
    CalendarEvent.external_id,
    CalendarEvent.data_payload,
    CalendarEvent.title,
    CalendarEvent.created_at,
    CalendarEvent.updated_at,
    CalendarEvent.organisation_id,
    CalendarEvent.integration_id,
    CalendarEvent.is_deleted,
    CalendarEvent.synced_at,
    CalendarEvent.source_modified_at,
    CalendarEvent.last_seen_at,
    CalendarEvent.organiser_email,
    CalendarEvent.organiser_name,
    CalendarEvent.calendar_external_id,
    CalendarEvent.description,
    CalendarEvent.location,
    CalendarEvent.start_at,
    CalendarEvent.end_at,
    CalendarEvent.is_all_day,
    CalendarEvent.status,
    CalendarEvent.conference_link,
    CalendarEvent.online_meeting_id,
    CalendarEvent.online_meeting_provider,
    CalendarEvent.timezone,
    CalendarEvent.attendee_count,
)


@dataclass(frozen=True)
class EventIdentity:
    provider: str
    external_id: str
    occurrence_anchor: str
    original_start_at: datetime | None


@dataclass
class DedupStats:
    duplicate_groups: int = 0
    events_deleted: int = 0
    events_updated: int = 0
    user_states_moved: int = 0
    associations_deleted: int = 0
    meetings_repointed: int = 0
    meetings_deleted: int = 0
    deal_jobs_repointed: int = 0
    orphaned_events_found: int = 0
    events_missing_identity: int = 0


@dataclass
class DuplicateGroup:
    """A group of CalendarEvent rows that represent the same real meeting occurrence."""

    identity: EventIdentity
    events: list[CalendarEvent] = field(default_factory=list)


@dataclass(frozen=True)
class EventArtifacts:
    has_meeting: bool = False
    meeting_status: str | None = None
    has_full_transcript: bool = False
    transcript_segments: int = 0


def _parse_iso_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", ISO_UTC_SUFFIX))


def _parse_iso_date(value: str) -> datetime:
    return datetime.fromisoformat(f"{value}T00:00:00{ISO_UTC_SUFFIX}")


def _format_occurrence_anchor(value: datetime | None) -> str:
    if value is None:
        return ""
    normalized = value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return normalized.isoformat().replace("+00:00", "Z")


def _extract_identity(payload: dict | None) -> EventIdentity | None:
    if not isinstance(payload, dict):
        return None

    google_uid = payload.get("iCalUID")
    if isinstance(google_uid, str) and google_uid:
        original_start = payload.get("originalStartTime")
        if isinstance(original_start, dict):
            date_time = original_start.get("dateTime")
            if isinstance(date_time, str) and date_time:
                parsed = _parse_iso_datetime(date_time)
                return EventIdentity("google", google_uid, _format_occurrence_anchor(parsed), parsed)

            date_value = original_start.get("date")
            if isinstance(date_value, str) and date_value:
                parsed = _parse_iso_date(date_value)
                return EventIdentity("google", google_uid, _format_occurrence_anchor(parsed), parsed)

        return EventIdentity("google", google_uid, "", None)

    # Microsoft iCalUId is unique per occurrence — no anchor needed.
    microsoft_uid = payload.get("iCalUId")
    if isinstance(microsoft_uid, str) and microsoft_uid:
        return EventIdentity("outlook", microsoft_uid, "", None)

    return None


def _association_score(assoc: CalendarEventDealAssociation) -> int:
    score = 0
    for status_field in (
        assoc.meeting_prep_status,
        assoc.meeting_summary_status,
        assoc.meeting_followup_status,
    ):
        status_str = status_field if isinstance(status_field, str) else status_field.value
        if status_str == GenerationStatus.COMPLETED.value:
            score += 10
        elif status_str == GenerationStatus.GENERATING.value:
            score += 2
        elif status_str == GenerationStatus.FAILED.value:
            score += 1

    for data_field in (
        assoc.meeting_prep_data,
        assoc.meeting_summary_data,
        assoc.meeting_followup_data,
    ):
        if data_field is not None:
            score += 5
    return score


def _event_score(
    event: CalendarEvent,
    associations: list[CalendarEventDealAssociation],
    artifacts: EventArtifacts,
) -> int:
    score = sum(_association_score(a) for a in associations if a.event_id == event.event_id)

    # Prefer the copy that already has downloaded transcript/meeting artifacts attached.
    if artifacts.has_meeting:
        score += 25
    if artifacts.meeting_status == MeetingStatus.READY.value:
        score += 40
    elif artifacts.meeting_status in (MeetingStatus.PROCESSING.value, MeetingStatus.FETCHING.value):
        score += 20
    if artifacts.has_full_transcript:
        score += 40
    if artifacts.transcript_segments > 0:
        score += 20 + min(artifacts.transcript_segments, 20)

    return score


def _pick_winner(
    events: list[CalendarEvent],
    all_associations: list[CalendarEventDealAssociation],
    artifacts_by_event_id: dict[UUID, EventArtifacts],
) -> tuple[CalendarEvent, list[CalendarEvent]]:
    scored = [
        (e, _event_score(e, all_associations, artifacts_by_event_id.get(e.event_id, EventArtifacts())))
        for e in events
    ]
    scored.sort(key=lambda x: (-x[1], x[0].created_at))
    winner = scored[0][0]
    losers = [e for e, _ in scored if e.event_id != winner.event_id]
    return winner, losers


async def _load_event_artifacts(
    session,
    event_ids: list[UUID],
) -> dict[UUID, EventArtifacts]:
    if not event_ids:
        return {}

    stmt = select(Meeting).where(col(Meeting.calendar_event_id).in_(event_ids))
    result = await session.execute(stmt)
    meetings = result.scalars().all()

    artifacts_by_event_id: dict[UUID, EventArtifacts] = {
        meeting.calendar_event_id: EventArtifacts(
            has_meeting=True,
            meeting_status=meeting.status if isinstance(meeting.status, str) else meeting.status.value,
            has_full_transcript=bool(meeting.full_transcript),
            transcript_segments=0,
        )
        for meeting in meetings
    }

    meeting_ids = [meeting.meeting_id for meeting in meetings]
    if not meeting_ids:
        return artifacts_by_event_id

    segment_stmt = (
        select(TranscriptSegment.meeting_id, func.count(TranscriptSegment.segment_id))
        .where(col(TranscriptSegment.meeting_id).in_(meeting_ids))
        .group_by(TranscriptSegment.meeting_id)
    )
    segment_result = await session.execute(segment_stmt)
    segment_count_by_meeting_id = {row[0]: int(row[1]) for row in segment_result.all()}

    for meeting in meetings:
        current = artifacts_by_event_id[meeting.calendar_event_id]
        artifacts_by_event_id[meeting.calendar_event_id] = EventArtifacts(
            has_meeting=current.has_meeting,
            meeting_status=current.meeting_status,
            has_full_transcript=current.has_full_transcript,
            transcript_segments=segment_count_by_meeting_id.get(meeting.meeting_id, 0),
        )

    return artifacts_by_event_id


async def _find_duplicate_groups(session, organisation_id: UUID) -> tuple[list[DuplicateGroup], int]:
    groups: list[DuplicateGroup] = []
    stmt = select(CalendarEvent).options(_SAFE_LOAD).where(col(CalendarEvent.organisation_id) == organisation_id)
    result = await session.execute(stmt)
    all_events = result.scalars().all()

    identity_map: dict[tuple[str, str], DuplicateGroup] = {}
    missing_identity = 0

    for event in all_events:
        identity = _extract_identity(event.data_payload)
        if identity is None:
            missing_identity += 1
            continue

        key = (identity.external_id, identity.occurrence_anchor)
        if key not in identity_map:
            identity_map[key] = DuplicateGroup(identity=identity)
        identity_map[key].events.append(event)

    for group in identity_map.values():
        if len(group.events) > 1:
            groups.append(group)

    return groups, missing_identity


async def _merge_user_states(session, winner: CalendarEvent, loser: CalendarEvent, stats: DedupStats) -> None:
    stmt = select(col(CalendarEventUserState.integration_id)).where(
        col(CalendarEventUserState.event_id) == winner.event_id,
    )
    result = await session.execute(stmt)
    winner_integration_ids = {row[0] for row in result.all()}

    stmt = select(CalendarEventUserState).where(col(CalendarEventUserState.event_id) == loser.event_id)
    result = await session.execute(stmt)
    loser_states = result.scalars().all()

    for state in loser_states:
        if state.integration_id not in winner_integration_ids:
            state.event_id = winner.event_id
            state.updated_at = datetime.now(UTC)
            session.add(state)
            stats.user_states_moved += 1
        else:
            await session.delete(state)


async def _delete_loser_deal_associations(
    session,
    loser: CalendarEvent,
    stats: DedupStats,
) -> None:
    """Delete all deal associations belonging to the loser event."""
    stmt = select(CalendarEventDealAssociation).where(
        col(CalendarEventDealAssociation.event_id) == loser.event_id,
    )
    result = await session.execute(stmt)
    loser_assocs = result.scalars().all()

    for loser_assoc in loser_assocs:
        await session.delete(loser_assoc)
        stats.associations_deleted += 1


async def _merge_meetings(session, winner: CalendarEvent, loser: CalendarEvent, stats: DedupStats) -> None:
    stmt = select(Meeting).where(col(Meeting.calendar_event_id) == winner.event_id)
    result = await session.execute(stmt)
    winner_meeting = result.scalar_one_or_none()

    stmt = select(Meeting).where(col(Meeting.calendar_event_id) == loser.event_id)
    result = await session.execute(stmt)
    loser_meeting = result.scalar_one_or_none()

    if not loser_meeting:
        return

    if winner_meeting:
        await session.execute(
            delete(TranscriptSegment).where(col(TranscriptSegment.meeting_id) == loser_meeting.meeting_id)
        )
        await session.delete(loser_meeting)
        stats.meetings_deleted += 1
    else:
        loser_meeting.calendar_event_id = winner.event_id
        session.add(loser_meeting)
        stats.meetings_repointed += 1


async def _repoint_deal_jobs(session, winner: CalendarEvent, loser: CalendarEvent, stats: DedupStats) -> None:
    stmt = update(DealJobs).where(col(DealJobs.event_id) == loser.event_id).values(event_id=winner.event_id)
    result = await session.execute(stmt)
    stats.deal_jobs_repointed += result.rowcount or 0


async def _delete_loser(session, loser: CalendarEvent) -> None:
    await session.execute(delete(CalendarAttendee).where(col(CalendarAttendee.event_id) == loser.event_id))
    await session.execute(delete(CalendarEventUserState).where(col(CalendarEventUserState.event_id) == loser.event_id))
    await session.delete(loser)


async def _find_orphaned_events(session, organisation_id: UUID) -> list[CalendarEvent]:
    subquery = select(col(CalendarEventUserState.event_id)).distinct()
    stmt = select(CalendarEvent).options(_SAFE_LOAD).where(
        col(CalendarEvent.organisation_id) == organisation_id,
        col(CalendarEvent.event_id).notin_(subquery),
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _has_identity_columns(session) -> bool:
    """Check if the new identity columns exist (migration 037 has run)."""
    result = await session.execute(
        select(func.count())
        .select_from(sa.text("information_schema.columns"))
        .where(
            sa.text("table_name = 'calendar_events' AND column_name = 'occurrence_anchor'")
        )
    )
    return (result.scalar() or 0) > 0


def _apply_identity(event: CalendarEvent, identity: EventIdentity) -> bool:
    changed = False
    if event.external_id != identity.external_id:
        event.external_id = identity.external_id
        changed = True
    if hasattr(event, "provider") and event.provider != identity.provider:
        event.provider = identity.provider
        changed = True
    if hasattr(event, "occurrence_anchor") and event.occurrence_anchor != identity.occurrence_anchor:
        event.occurrence_anchor = identity.occurrence_anchor
        changed = True
    if hasattr(event, "original_start_at") and event.original_start_at != identity.original_start_at:
        event.original_start_at = identity.original_start_at
        changed = True
    if changed:
        event.updated_at = datetime.now(UTC)
    return changed


async def _backfill_identity_fields(session, organisation_id: UUID, stats: DedupStats) -> None:
    stmt = select(CalendarEvent).where(col(CalendarEvent.organisation_id) == organisation_id)
    result = await session.execute(stmt)
    events = result.scalars().all()

    for event in events:
        identity = _extract_identity(event.data_payload)
        if identity is None:
            continue
        if _apply_identity(event, identity):
            session.add(event)
            stats.events_updated += 1


async def process_org(organisation_id: UUID, execute: bool) -> DedupStats:
    stats = DedupStats()

    async for session in acquire_org_async_session(organisation_id):
        groups, missing_identity = await _find_duplicate_groups(session, organisation_id)
        stats.duplicate_groups = len(groups)
        stats.events_missing_identity = missing_identity

        if groups:
            print(f"  Found {len(groups)} duplicate group(s)")
        if missing_identity:
            print(f"  WARNING: {missing_identity} event(s) have no shared UID identity and will be skipped")

        for group in groups:
            print(
                f"    Shared UID: {group.identity.external_id[:50]}... "
                f"anchor={group.identity.occurrence_anchor or '<single>'} "
                f"copies={len(group.events)} provider={group.identity.provider}"
            )

            event_ids = [e.event_id for e in group.events]
            stmt = select(CalendarEventDealAssociation).where(col(CalendarEventDealAssociation.event_id).in_(event_ids))
            result = await session.execute(stmt)
            all_associations = list(result.scalars().all())
            artifacts_by_event_id = await _load_event_artifacts(session, event_ids)

            winner, losers = _pick_winner(group.events, all_associations, artifacts_by_event_id)
            print(f"      Winner: {winner.event_id} (title={winner.title})")
            for loser in losers:
                print(f"      Loser:  {loser.event_id} (title={loser.title})")

            if not execute:
                stats.events_deleted += len(losers)
                continue

            for loser in losers:
                await _merge_user_states(session, winner, loser, stats)
                await _delete_loser_deal_associations(session, loser, stats)
                await _merge_meetings(session, winner, loser, stats)
                await _repoint_deal_jobs(session, winner, loser, stats)
                await _delete_loser(session, loser)
                stats.events_deleted += 1
                print(f"      Deleted loser: {loser.event_id}")

        orphaned = await _find_orphaned_events(session, organisation_id)
        stats.orphaned_events_found = len(orphaned)
        if orphaned:
            print(f"  Found {len(orphaned)} orphaned event(s) with no user_states")
            for event in orphaned:
                print(f"    Orphan: {event.event_id} title={event.title} external_id={event.external_id}")

        if execute:
            has_new_cols = await _has_identity_columns(session)
            if has_new_cols:
                await _backfill_identity_fields(session, organisation_id, stats)
            else:
                print("  Skipping identity backfill (migration 037 not yet applied)")
            await session.commit()

    return stats


async def get_all_org_ids() -> list[UUID]:
    from src.domain.models.master_tables.organisation import Organisation

    async for session in acquire_master_async_session():
        stmt = select(col(Organisation.organisation_id)).where(col(Organisation.status) == "APPROVED")
        result = await session.execute(stmt)
        return [row[0] for row in result.all()]
    return []


async def main(execute: bool = False, org_id: str | None = None) -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

    mode = "EXECUTE" if execute else "DRY RUN"
    scope = f"org {org_id}" if org_id else "ALL orgs"
    print()
    print("=" * 90)
    print(f"  DEDUPLICATE CALENDAR EVENTS (shared UID + occurrence anchor) - {mode}")
    print(f"  Scope: {scope}")
    print("=" * 90)
    print()

    org_ids = [UUID(org_id)] if org_id else await get_all_org_ids()
    print(f"Processing {len(org_ids)} organisation(s)")
    print()

    total = DedupStats()
    for oid in org_ids:
        print()
        print(f"--- Org: {oid} ---")
        try:
            stats = await process_org(oid, execute)
            total.duplicate_groups += stats.duplicate_groups
            total.events_deleted += stats.events_deleted
            total.events_updated += stats.events_updated
            total.user_states_moved += stats.user_states_moved
            total.associations_deleted += stats.associations_deleted
            total.meetings_repointed += stats.meetings_repointed
            total.meetings_deleted += stats.meetings_deleted
            total.deal_jobs_repointed += stats.deal_jobs_repointed
            total.orphaned_events_found += stats.orphaned_events_found
            total.events_missing_identity += stats.events_missing_identity

            if stats.duplicate_groups == 0 and stats.events_updated == 0:
                print("  No duplicates or identity updates needed")
            else:
                print(
                    f"  Result: {stats.duplicate_groups} duplicate group(s), "
                    f"{stats.events_deleted} event(s) deleted, "
                    f"{stats.events_updated} event(s) identity-updated, "
                    f"{stats.events_missing_identity} missing-identity event(s), "
                    f"{stats.orphaned_events_found} orphaned event(s)"
                )
        except Exception as e:
            print(f"  ERROR: {e}")
            logger.error("Failed to process org %s: %s", oid, e, exc_info=True)

    print()
    print("=" * 90)
    print(
        f"TOTAL: {total.duplicate_groups} duplicate group(s), "
        f"{total.events_deleted} event(s) deleted, "
        f"{total.events_updated} event(s) updated"
    )
    print(
        f"  user_states moved: {total.user_states_moved}, "
        f"associations deleted: {total.associations_deleted}, "
        f"meetings repointed: {total.meetings_repointed}, "
        f"meetings deleted: {total.meetings_deleted}, "
        f"deal_jobs repointed: {total.deal_jobs_repointed}, "
        f"orphaned events: {total.orphaned_events_found}, "
        f"missing identity: {total.events_missing_identity}"
    )
    if not execute:
        print("DRY RUN - no changes made. Run with --execute to apply.")
    print("=" * 90)
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Deduplicate calendar events stored with mailbox-specific IDs"
    )
    parser.add_argument("--execute", action="store_true", help="Actually apply changes (default: dry run)")
    parser.add_argument(
        "--org-id",
        type=lambda value: str(UUID(value)),
        help="Process a single organisation (default: all approved orgs)",
    )
    args = parser.parse_args()
    asyncio.run(main(execute=args.execute, org_id=args.org_id))
