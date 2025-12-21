"""Deal timeline sync service for batch timeline operations.

This service handles batch creation and synchronization of timeline events
from existing associations. It's designed for:
- Backfilling timeline events for existing data
- Repairing/reconciling timeline data
- Batch operations that span multiple deals

For real-time timeline creation, use inline creation in the matcher services
(DealEmailMatcher, DealMeetingMatcher) within the same transaction.

See docs/Development Documentation/deal-timeline-feature.md for design details.
"""

from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from src.domain.models.organisation_tables.sync.calendar import (
    CalendarEvent,
    CalendarEventDealAssociation,
)
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.domain.models.organisation_tables.sync.email_models import (
    DealEmailAssociation,
    Email,
)
from src.domain.models.organisation_tables.sync.timeline_models import (
    DealTimelineEvent,
    TimelineEventSource,
    TimelineEventType,
)
from src.infra.utils.logger import get_logger

logger = get_logger("deal_timeline_sync_service")


def _enum_value(val: Enum | str | None) -> str | None:
    """Safely extract string value from an enum or pass through string.

    Database may return enum fields as either enum instances or raw strings
    depending on how the model loads data.
    """
    if val is None:
        return None
    if isinstance(val, str):
        return val
    return val.value


class DealTimelineSyncService:
    """Service for batch timeline sync operations.

    Use this service for:
    - Backfilling timeline events from existing associations
    - Syncing timeline for a single deal
    - Org-wide timeline reconciliation

    For real-time creation during association matching, use the helper
    methods directly in DealEmailMatcher/DealMeetingMatcher.
    """

    def __init__(self, session: AsyncSession, organisation_id: UUID):
        """Initialize the service with an org-scoped session.

        Args:
            session: Async database session scoped to the organisation
            organisation_id: UUID of the organisation
        """
        self.session = session
        self.organisation_id = organisation_id

    # -------------------------------------------------------------------------
    # Batch Sync Methods
    # -------------------------------------------------------------------------

    async def sync_deal_timeline(
        self,
        deal_id: UUID,
        event_types: list[TimelineEventType] | None = None,
    ) -> dict[str, int]:
        """
        Sync all timeline events for a single deal.

        Creates timeline events for all associations that don't already have them.
        Uses the unique constraint to avoid duplicates.

        Args:
            deal_id: Deal UUID
            event_types: Optional filter - only sync specific event types

        Returns:
            Stats dict: {emails_synced, meetings_synced, total_created, total_skipped}
        """
        results = {
            "emails_synced": 0,
            "meetings_synced": 0,
            "total_created": 0,
            "total_skipped": 0,
        }

        # Fetch existing timeline events for this deal (for dedup)
        existing_keys = await self._get_existing_event_keys(deal_id)

        if not event_types or TimelineEventType.EMAIL in event_types:
            email_stats = await self._sync_email_timeline_events(deal_id, existing_keys)
            results["emails_synced"] = email_stats["synced"]
            results["total_created"] += email_stats["created"]
            results["total_skipped"] += email_stats["skipped"]

        if not event_types or TimelineEventType.MEETING in event_types:
            meeting_stats = await self._sync_meeting_timeline_events(deal_id, existing_keys)
            results["meetings_synced"] = meeting_stats["synced"]
            results["total_created"] += meeting_stats["created"]
            results["total_skipped"] += meeting_stats["skipped"]

        await self.session.flush()

        logger.info(
            f"Timeline sync for deal {deal_id}: "
            f"created={results['total_created']}, skipped={results['total_skipped']}"
        )
        return results

    async def sync_organisation_timeline(
        self,
        deal_ids: list[UUID] | None = None,
        batch_size: int = 50,
    ) -> dict[str, int]:
        """
        Sync timeline events for all deals in the organisation.

        Processes in batches to manage memory and transaction size.

        Args:
            deal_ids: Optional list of specific deal IDs to sync
            batch_size: Number of deals to process per batch

        Returns:
            Aggregate stats dict
        """
        results = {
            "deals_processed": 0,
            "emails_synced": 0,
            "meetings_synced": 0,
            "total_created": 0,
            "total_skipped": 0,
        }

        # Get deal IDs to process
        if deal_ids:
            ids_to_process = deal_ids
        else:
            stmt = select(CRMDeal.deal_id).where(
                CRMDeal.organisation_id == self.organisation_id,
                CRMDeal.is_deleted.is_(False),
            )
            result = await self.session.execute(stmt)
            ids_to_process = list(result.scalars().all())

        logger.info(
            f"Syncing timeline for {len(ids_to_process)} deals in org {self.organisation_id}"
        )

        # Process in batches
        for i in range(0, len(ids_to_process), batch_size):
            batch = ids_to_process[i : i + batch_size]

            for deal_id in batch:
                deal_stats = await self.sync_deal_timeline(deal_id)
                results["deals_processed"] += 1
                results["emails_synced"] += deal_stats["emails_synced"]
                results["meetings_synced"] += deal_stats["meetings_synced"]
                results["total_created"] += deal_stats["total_created"]
                results["total_skipped"] += deal_stats["total_skipped"]

            # Commit after each batch
            await self.session.commit()
            logger.debug(f"Committed timeline sync batch {i // batch_size + 1}")

        logger.info(
            f"Organisation timeline sync complete: {results['deals_processed']} deals, "
            f"{results['total_created']} events created"
        )
        return results

    # -------------------------------------------------------------------------
    # Private Helpers
    # -------------------------------------------------------------------------

    async def _get_existing_event_keys(self, deal_id: UUID) -> set[tuple[str, str]]:
        """Get existing (event_type, source_record_id) pairs for a deal."""
        stmt = select(
            DealTimelineEvent.event_type,
            DealTimelineEvent.source_record_id,
        ).where(
            DealTimelineEvent.organisation_id == self.organisation_id,
            DealTimelineEvent.deal_id == deal_id,
        )
        result = await self.session.execute(stmt)
        # Normalise event_type to a string so callers can safely compare keys
        # regardless of whether SQLAlchemy materialises it as an Enum or str.
        keys: set[tuple[str, str]] = set()
        for event_type, source_record_id in result.all():
            event_type_str = _enum_value(event_type) or str(event_type)
            keys.add((event_type_str, source_record_id))
        return keys

    async def _insert_timeline_event_ignore_conflict(self, event: DealTimelineEvent) -> bool:
        """Insert a timeline event, ignoring duplicates.

        This makes batch sync resilient to concurrent executions (or overlapping
        backfills) without relying on a precomputed key set.

        Returns:
            True if inserted, False if skipped due to unique conflict.
        """
        values: dict[str, Any] = event.model_dump()  # SQLModel / Pydantic v2

        # Ensure enums are stored as strings
        values["event_type"] = _enum_value(values.get("event_type"))
        values["event_source"] = _enum_value(values.get("event_source"))

        stmt = (
            insert(DealTimelineEvent)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_timeline_event_source")
        )
        result = await self.session.execute(stmt)
        return (result.rowcount or 0) > 0

    async def _sync_email_timeline_events(
        self,
        deal_id: UUID,
        existing_keys: set[tuple[str, str]],
    ) -> dict[str, int]:
        """Batch sync email associations → timeline events."""
        stats = {"synced": 0, "created": 0, "skipped": 0}

        # Fetch all email associations for this deal with email data
        stmt = (
            select(DealEmailAssociation, Email)
            .outerjoin(
                Email,
                (DealEmailAssociation.internet_message_id == Email.internet_message_id)
                & (Email.organisation_id == self.organisation_id),
            )
            .where(
                DealEmailAssociation.organisation_id == self.organisation_id,
                DealEmailAssociation.deal_id == deal_id,
            )
        )
        result = await self.session.execute(stmt)

        for assoc, email in result:
            stats["synced"] += 1
            key = (TimelineEventType.EMAIL.value, str(assoc.association_id))

            if key in existing_keys:
                stats["skipped"] += 1
                continue

            # Create timeline event
            timeline_event = build_email_timeline_event(
                organisation_id=self.organisation_id,
                deal_id=deal_id,
                association=assoc,
                email=email,
            )
            created = await self._insert_timeline_event_ignore_conflict(timeline_event)
            if created:
                stats["created"] += 1
                existing_keys.add(key)
            else:
                stats["skipped"] += 1
                existing_keys.add(key)

        return stats

    async def _sync_meeting_timeline_events(
        self,
        deal_id: UUID,
        existing_keys: set[tuple[str, str]],
    ) -> dict[str, int]:
        """Batch sync meeting associations → timeline events."""
        stats = {"synced": 0, "created": 0, "skipped": 0}

        # Fetch all meeting associations for this deal with event data
        # Use selectinload for attendees to avoid lazy-load issues in async context
        stmt = (
            select(CalendarEventDealAssociation, CalendarEvent)
            .outerjoin(
                CalendarEvent,
                (CalendarEventDealAssociation.event_id == CalendarEvent.event_id)
                & (CalendarEvent.organisation_id == self.organisation_id),
            )
            .options(selectinload(CalendarEvent.attendees))
            .where(
                CalendarEventDealAssociation.organisation_id == self.organisation_id,
                CalendarEventDealAssociation.deal_id == deal_id,
            )
        )
        result = await self.session.execute(stmt)

        for assoc, event in result:
            stats["synced"] += 1
            key = (TimelineEventType.MEETING.value, str(assoc.association_id))

            if key in existing_keys:
                stats["skipped"] += 1
                continue

            # Create timeline event
            timeline_event = build_meeting_timeline_event(
                organisation_id=self.organisation_id,
                deal_id=deal_id,
                association=assoc,
                event=event,
            )
            created = await self._insert_timeline_event_ignore_conflict(timeline_event)
            if created:
                stats["created"] += 1
                existing_keys.add(key)
            else:
                stats["skipped"] += 1
                existing_keys.add(key)

        return stats


# =============================================================================
# Standalone Builder Functions (used by matchers for inline creation)
# =============================================================================


def build_email_timeline_event(
    organisation_id: UUID,
    deal_id: UUID,
    association: DealEmailAssociation,
    email: Email | None = None,
    event_source: TimelineEventSource = TimelineEventSource.EMAIL_GMAIL,
) -> DealTimelineEvent:
    """
    Build a timeline event for an email association.

    This is a pure function that creates a DealTimelineEvent object.
    Called by DealEmailMatcher for inline creation and by sync service for batch.

    Args:
        organisation_id: Org UUID
        deal_id: Deal UUID
        association: The DealEmailAssociation record
        email: Optional Email record for additional context
        event_source: Source system (EMAIL_GMAIL, EMAIL_OUTLOOK)

    Returns:
        DealTimelineEvent (not yet added to session)
    """
    # Extract data - prefer email record, fall back to association
    subject = association.email_subject or "(No Subject)"
    email_date = association.email_date or association.created_at
    sender_name = None
    sender_email = None

    if email:
        subject = email.subject or subject
        email_date = email.received_at or email_date
        sender_name = email.from_name
        sender_email = email.from_email

    # Build subtitle
    subtitle = None
    if sender_name:
        subtitle = f"From: {sender_name}"
    elif sender_email:
        subtitle = f"From: {sender_email}"

    # Build participant preview
    participant_names: list[str] = []
    participant_count = 0
    if email:
        if email.from_name:
            participant_names.append(email.from_name)
        elif email.from_email:
            participant_names.append(email.from_email)

        to_count = len(email.to_emails) if email.to_emails else 0
        cc_count = len(email.cc_emails) if email.cc_emails else 0
        participant_count = 1 + to_count + cc_count  # sender + recipients

        # Add first few recipients to preview
        for addr in (email.to_emails or [])[:2]:
            participant_names.append(addr)

    # Build metadata
    metadata: dict[str, Any] = {
        "internet_message_id": association.internet_message_id,
    }
    if email:
        metadata["has_attachments"] = email.has_attachments
        metadata["thread_id"] = email.thread_id

    return DealTimelineEvent(
        organisation_id=organisation_id,
        deal_id=deal_id,
        event_type=TimelineEventType.EMAIL,
        event_source=event_source,
        event_timestamp=email_date,
        source_record_id=str(association.association_id),
        title=subject[:500] if subject else "(No Subject)",
        subtitle=subtitle[:500] if subtitle else None,
        direction=_enum_value(association.direction),
        status=_enum_value(association.status),
        participant_names=participant_names[:4],  # Max 4 for preview
        participant_count=participant_count,
        event_metadata=metadata,
    )


def build_meeting_timeline_event(
    organisation_id: UUID,
    deal_id: UUID,
    association: CalendarEventDealAssociation,
    event: CalendarEvent | None = None,
    event_source: TimelineEventSource = TimelineEventSource.CALENDAR_GOOGLE,
) -> DealTimelineEvent:
    """
    Build a timeline event for a meeting association.

    This is a pure function that creates a DealTimelineEvent object.
    Called by DealMeetingMatcher for inline creation and by sync service for batch.

    Args:
        organisation_id: Org UUID
        deal_id: Deal UUID
        association: The CalendarEventDealAssociation record
        event: Optional CalendarEvent for additional context
        event_source: Source system (CALENDAR_GOOGLE, CALENDAR_OUTLOOK)

    Returns:
        DealTimelineEvent (not yet added to session)
    """
    # Extract data - prefer event record, fall back to association
    title = "Meeting"
    start_at = association.created_at
    duration_minutes = None
    participant_count = 0

    if event:
        title = event.title or title
        start_at = event.start_at or start_at
        participant_count = event.attendee_count or 0

        if event.start_at and event.end_at:
            duration_minutes = int((event.end_at - event.start_at).total_seconds() / 60)

    # Build subtitle
    subtitle = None
    if duration_minutes:
        subtitle = f"Duration: {duration_minutes} mins"

    # Build participant preview
    participant_names: list[str] = []
    if event:
        if event.organiser_name:
            participant_names.append(event.organiser_name)
        elif event.organiser_email:
            participant_names.append(event.organiser_email)

        # Add attendee names if available (from attendees relationship)
        attendees = getattr(event, "attendees", None)
        if attendees:
            for att in attendees[:3]:
                name = getattr(att, "display_name", None) or getattr(att, "email", None)
                if name and name not in participant_names:
                    participant_names.append(name)

    # Build metadata
    metadata: dict[str, Any] = {}
    if duration_minutes:
        metadata["duration_minutes"] = duration_minutes
    if event:
        if event.location:
            metadata["location"] = event.location
        if event.conference_link:
            metadata["conference_link"] = event.conference_link
    if association.meeting_prep_status:
        metadata["meeting_prep_status"] = _enum_value(association.meeting_prep_status)
    if association.meeting_summary_status:
        metadata["meeting_summary_status"] = _enum_value(association.meeting_summary_status)

    return DealTimelineEvent(
        organisation_id=organisation_id,
        deal_id=deal_id,
        event_type=TimelineEventType.MEETING,
        event_source=event_source,
        event_timestamp=start_at,
        source_record_id=str(association.association_id),
        title=title[:500] if title else "Meeting",
        subtitle=subtitle[:500] if subtitle else None,
        direction=None,  # Meetings don't have direction
        status=_enum_value(association.status),
        participant_names=participant_names[:4],  # Max 4 for preview
        participant_count=participant_count,
        event_metadata=metadata,
    )
