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

from datetime import UTC, datetime
from enum import Enum
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import QueryableAttribute, selectinload
from sqlalchemy.sql.schema import Table
from src.domain.models.organisation_tables.sync.calendar import (
    CalendarEvent,
    CalendarEventDealAssociation,
)
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealStage,
    CRMDealStageHistory,
)
from src.domain.models.organisation_tables.sync.email_models import (
    DealEmailAssociation,
    Email,
)
from src.domain.models.organisation_tables.sync.integration_models import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.timeline_models import (
    DealTimelineEvent,
    TimelineEventSource,
    TimelineEventType,
)
from src.infra.utils.logger import get_logger
from src.service.crm.timeline_event_source_resolver import (
    resolve_crm_event_source,
    resolve_email_event_source,
    resolve_meeting_event_source,
)

logger = get_logger("deal_timeline_sync_service")


def _table(model: Any) -> Table:
    """Safely extract the SQLAlchemy table for a mapped model."""
    table_name = getattr(model, "__tablename__", None)
    if table_name is None:
        raise ValueError(f"Model {model} missing __tablename__ for table lookup")

    table = model.metadata.tables.get(table_name)
    if table is None:
        raise ValueError(f"Table not found for model {model}")

    return cast(Table, table)


def _enum_value(val: Enum | str | None) -> str | None:
    """Safely extract string value from an enum or pass through string.

    Database may return enum fields as either enum instances or raw strings
    depending on how the model loads data.
    """
    if val is None:
        return None
    if isinstance(val, str):
        return val
    return str(val.value)


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
        self._stage_lookup_cache: dict[str, dict[str, Any]] | None = None

    async def _get_stage_lookup(self) -> dict[str, dict[str, Any]]:
        """Build a lookup map of stage external_id -> stage info.

        Returns dict mapping external_id to:
        {
            "stage_id": UUID (internal),
            "name": str (display name),
            "label": str | None (optional label),
        }
        """
        if self._stage_lookup_cache is not None:
            return self._stage_lookup_cache

        stage_table: Table = _table(CRMDealStage)
        stmt = select(CRMDealStage).where(
            stage_table.c.organisation_id == self.organisation_id,
            stage_table.c.is_deleted.is_(False),
        )
        result = await self.session.execute(stmt)
        stages = result.scalars().all()

        self._stage_lookup_cache = {
            stage.external_id: {
                "stage_id": stage.stage_id,
                "name": stage.name,
                "label": stage.label,
            }
            for stage in stages
        }
        return self._stage_lookup_cache

    def _resolve_stage_label(
        self,
        stage_lookup: dict[str, dict[str, Any]],
        external_id: str | None,
    ) -> tuple[str | None, str | None]:
        """Resolve stage external_id to (internal_stage_id, display_label).

        Returns:
            Tuple of (stage_id as string, display_label)
            Falls back to external_id if not found in lookup
        """
        if not external_id:
            return None, None

        stage_info = stage_lookup.get(external_id)
        if stage_info:
            # Use label if available, otherwise name
            display_label = stage_info.get("label") or stage_info.get("name")
            stage_id = str(stage_info["stage_id"])
            return stage_id, display_label

        # Fallback: return external_id as both ID and label
        return None, external_id

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
            Stats dict with counts for each event type synced
        """
        results = {
            "emails_synced": 0,
            "meetings_synced": 0,
            "stage_changes_synced": 0,
            "deal_created_synced": 0,
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

        if not event_types or TimelineEventType.STAGE_CHANGE in event_types:
            stage_stats = await self._sync_stage_change_timeline_events(deal_id, existing_keys)
            results["stage_changes_synced"] = stage_stats["synced"]
            results["total_created"] += stage_stats["created"]
            results["total_skipped"] += stage_stats["skipped"]

        if not event_types or TimelineEventType.DEAL_CREATED in event_types:
            created_stats = await self._sync_deal_created_timeline_event(deal_id, existing_keys)
            results["deal_created_synced"] = created_stats["synced"]
            results["total_created"] += created_stats["created"]
            results["total_skipped"] += created_stats["skipped"]

        await self.session.flush()

        logger.info(
            f"Timeline sync for deal {deal_id}: "
            f"created={results['total_created']}, skipped={results['total_skipped']}"
        )
        return results

    async def sync_organisation_timeline(
        self,
        deal_ids: list[UUID] | None = None,
        event_types: list[TimelineEventType] | None = None,
        batch_size: int = 50,
    ) -> dict[str, int]:
        """
        Sync timeline events for all deals in the organisation.

        Processes in batches to manage memory and transaction size.

        Args:
            deal_ids: Optional list of specific deal IDs to sync
            event_types: Optional filter - only sync specific event types
            batch_size: Number of deals to process per batch

        Returns:
            Aggregate stats dict
        """
        results = {
            "deals_processed": 0,
            "emails_synced": 0,
            "meetings_synced": 0,
            "stage_changes_synced": 0,
            "deal_created_synced": 0,
            "total_created": 0,
            "total_skipped": 0,
        }

        # Get deal IDs to process
        if deal_ids:
            ids_to_process = deal_ids
        else:
            deal_table: Table = _table(CRMDeal)
            stmt = select(deal_table.c.deal_id).where(
                deal_table.c.organisation_id == self.organisation_id,
                deal_table.c.is_deleted.is_(False),
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
                deal_stats = await self.sync_deal_timeline(deal_id, event_types)
                results["deals_processed"] += 1
                results["emails_synced"] += deal_stats["emails_synced"]
                results["meetings_synced"] += deal_stats["meetings_synced"]
                results["stage_changes_synced"] += deal_stats["stage_changes_synced"]
                results["deal_created_synced"] += deal_stats["deal_created_synced"]
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
        t: Table = _table(DealTimelineEvent)
        stmt = select(
            t.c.event_type,
            t.c.source_record_id,
        ).where(
            t.c.organisation_id == self.organisation_id,
            t.c.deal_id == deal_id,
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
        return (getattr(result, "rowcount", 0) or 0) > 0

    async def _sync_email_timeline_events(
        self,
        deal_id: UUID,
        existing_keys: set[tuple[str, str]],
    ) -> dict[str, int]:
        """Batch sync email associations → timeline events."""
        stats = {"synced": 0, "created": 0, "skipped": 0}

        assoc_table: Table = _table(DealEmailAssociation)
        email_table: Table = _table(Email)
        integration_table: Table = _table(KnowledgeIntegration)

        # Fetch all email associations for this deal with email data.
        # Also pull the owning integration type so we can set event_source correctly.
        stmt = (
            select(DealEmailAssociation, Email, KnowledgeIntegration)
            .outerjoin(
                Email,
                (assoc_table.c.internet_message_id == email_table.c.internet_message_id)
                & (email_table.c.organisation_id == self.organisation_id),
            )
            .outerjoin(
                KnowledgeIntegration,
                email_table.c.integration_id == integration_table.c.integration_id,
            )
            .where(
                assoc_table.c.organisation_id == self.organisation_id,
                assoc_table.c.deal_id == deal_id,
            )
        )
        result = await self.session.execute(stmt)

        for assoc, email, integration in result:
            stats["synced"] += 1
            key = (TimelineEventType.EMAIL.value, str(assoc.association_id))

            if key in existing_keys:
                stats["skipped"] += 1
                continue

            # Create timeline event
            integration_type = getattr(integration, "integration_type", None)
            resolved_source = resolve_email_event_source(integration_type)
            timeline_event = build_email_timeline_event(
                organisation_id=self.organisation_id,
                deal_id=deal_id,
                association=assoc,
                email=email,
                event_source=resolved_source,
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

        assoc_table: Table = _table(CalendarEventDealAssociation)
        event_table: Table = _table(CalendarEvent)
        integration_table: Table = _table(KnowledgeIntegration)

        # Fetch all meeting associations for this deal with event data.
        # Use selectinload for attendees/user_states to avoid lazy-load issues in async context.
        attendees_attr: QueryableAttribute[Any] = CalendarEvent.attendees  # type: ignore[assignment]
        user_states_attr: QueryableAttribute[Any] = CalendarEvent.user_states  # type: ignore[assignment]
        stmt = (
            select(CalendarEventDealAssociation, CalendarEvent)
            .outerjoin(
                CalendarEvent,
                (assoc_table.c.event_id == event_table.c.event_id)
                & (event_table.c.organisation_id == self.organisation_id),
            )
            .options(selectinload(attendees_attr))
            .options(selectinload(user_states_attr))
            .where(
                assoc_table.c.organisation_id == self.organisation_id,
                assoc_table.c.deal_id == deal_id,
            )
        )
        result = await self.session.execute(stmt)
        rows = list(result.all())

        # Bulk fetch integration types for all integration_ids referenced by these events.
        integration_ids: set[UUID] = set()
        for _, event in rows:
            if not event:
                continue
            if getattr(event, "integration_id", None):
                integration_ids.add(event.integration_id)
            user_states = getattr(event, "user_states", None) or []
            for state in user_states:
                if getattr(state, "integration_id", None):
                    integration_ids.add(state.integration_id)

        integration_type_by_id: dict[UUID, Any] = {}
        if integration_ids:
            it_stmt = select(KnowledgeIntegration).where(
                integration_table.c.integration_id.in_(integration_ids)
            )
            it_result = await self.session.execute(it_stmt)
            integration_type_by_id = {
                row.integration_id: row.integration_type for row in it_result.scalars().all()
            }

        for assoc, event in rows:
            stats["synced"] += 1
            key = (TimelineEventType.MEETING.value, str(assoc.association_id))

            if key in existing_keys:
                stats["skipped"] += 1
                continue

            # Create timeline event
            calendar_integration_type = None
            if event:
                if getattr(event, "integration_id", None) in integration_type_by_id:
                    calendar_integration_type = integration_type_by_id.get(event.integration_id)
                else:
                    user_states = getattr(event, "user_states", None) or []
                    # Deterministic pick: sort by integration_id string.
                    sorted_states = sorted(
                        (s for s in user_states if getattr(s, "integration_id", None)),
                        key=lambda s: str(s.integration_id),
                    )
                    if sorted_states:
                        calendar_integration_type = integration_type_by_id.get(
                            sorted_states[0].integration_id
                        )

            resolved_source = resolve_meeting_event_source(event, calendar_integration_type)
            timeline_event = build_meeting_timeline_event(
                organisation_id=self.organisation_id,
                deal_id=deal_id,
                association=assoc,
                event=event,
                event_source=resolved_source,
            )
            created = await self._insert_timeline_event_ignore_conflict(timeline_event)
            if created:
                stats["created"] += 1
                existing_keys.add(key)
            else:
                stats["skipped"] += 1
                existing_keys.add(key)

        return stats

    async def _sync_stage_change_timeline_events(
        self,
        deal_id: UUID,
        existing_keys: set[tuple[str, str]],
    ) -> dict[str, int]:
        """Batch sync stage history records → timeline events."""
        stats = {"synced": 0, "created": 0, "skipped": 0}

        # Get stage lookup for resolving external_id -> name/label
        stage_lookup = await self._get_stage_lookup()

        history_table: Table = _table(CRMDealStageHistory)
        integration_table: Table = _table(KnowledgeIntegration)

        # Fetch all stage history records for this deal with integration data
        stmt = (
            select(CRMDealStageHistory, KnowledgeIntegration)
            .outerjoin(
                KnowledgeIntegration,
                history_table.c.integration_id == integration_table.c.integration_id,
            )
            .where(
                history_table.c.organisation_id == self.organisation_id,
                history_table.c.deal_id == deal_id,
                history_table.c.is_deleted.is_(False),
            )
        )
        result = await self.session.execute(stmt)

        for history, integration in result:
            stats["synced"] += 1
            key = (TimelineEventType.STAGE_CHANGE.value, str(history.external_id))

            if key in existing_keys:
                stats["skipped"] += 1
                continue

            # Resolve stage names/labels from lookup
            new_stage_id, new_stage_label = self._resolve_stage_label(
                stage_lookup, history.stage_external_id
            )
            old_stage_id, old_stage_label = self._resolve_stage_label(
                stage_lookup, history.from_stage_external_id
            )

            # Build history entry dict for builder function with resolved labels
            history_entry = {
                "external_id": history.external_id,
                "history_id": str(history.history_id),
                "deal_id": history.deal_id,
                # Store internal stage IDs (UUIDs)
                "stage_id": new_stage_id,
                "from_stage_id": old_stage_id,
                # Keep external IDs for reference
                "stage_external_id": history.stage_external_id,
                "from_stage_external_id": history.from_stage_external_id,
                "entered_at": history.entered_at,
                # Resolved display labels
                "stage_label": new_stage_label,
                "from_stage_label": old_stage_label,
            }

            # Resolve event source
            integration_type = getattr(integration, "integration_type", None)
            resolved_source = resolve_crm_event_source(integration_type)

            timeline_event = build_stage_change_timeline_event(
                organisation_id=self.organisation_id,
                deal_id=deal_id,
                history_entry=history_entry,
                event_source=resolved_source,
            )
            created = await self._insert_timeline_event_ignore_conflict(timeline_event)
            if created:
                stats["created"] += 1
                existing_keys.add(key)
            else:
                stats["skipped"] += 1
                existing_keys.add(key)

        return stats

    async def _sync_deal_created_timeline_event(
        self,
        deal_id: UUID,
        existing_keys: set[tuple[str, str]],
    ) -> dict[str, int]:
        """Create DEAL_CREATED timeline event for an existing deal."""
        stats = {"synced": 0, "created": 0, "skipped": 0}

        # Check if already exists
        key = (TimelineEventType.DEAL_CREATED.value, str(deal_id))
        if key in existing_keys:
            stats["synced"] = 1
            stats["skipped"] = 1
            return stats

        deal_table: Table = _table(CRMDeal)
        integration_table: Table = _table(KnowledgeIntegration)

        # Fetch deal with integration data
        stmt = (
            select(CRMDeal, KnowledgeIntegration)
            .outerjoin(
                KnowledgeIntegration,
                deal_table.c.integration_id == integration_table.c.integration_id,
            )
            .where(
                deal_table.c.organisation_id == self.organisation_id,
                deal_table.c.deal_id == deal_id,
            )
        )
        result = await self.session.execute(stmt)
        row = result.first()

        if not row:
            return stats

        deal, integration = row
        stats["synced"] = 1

        # Resolve event source
        integration_type = getattr(integration, "integration_type", None)
        resolved_source = resolve_crm_event_source(integration_type)

        timeline_event = build_deal_created_timeline_event(
            organisation_id=self.organisation_id,
            deal=deal,
            event_source=resolved_source,
        )
        created = await self._insert_timeline_event_ignore_conflict(timeline_event)
        if created:
            stats["created"] = 1
            existing_keys.add(key)
        else:
            stats["skipped"] = 1
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
    event_source: TimelineEventSource | None = None,
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

    resolved_source = event_source or TimelineEventSource.EMAIL_GMAIL

    return DealTimelineEvent(
        organisation_id=organisation_id,
        deal_id=deal_id,
        event_type=TimelineEventType.EMAIL,
        event_source=resolved_source,
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
    event_source: TimelineEventSource | None = None,
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

    resolved_source = event_source or TimelineEventSource.CALENDAR_GOOGLE

    return DealTimelineEvent(
        organisation_id=organisation_id,
        deal_id=deal_id,
        event_type=TimelineEventType.MEETING,
        event_source=resolved_source,
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


def build_stage_change_timeline_event(
    organisation_id: UUID,
    deal_id: UUID,
    history_entry: dict[str, Any],
    event_source: TimelineEventSource | None = None,
    deal_context: dict[str, Any] | None = None,
) -> DealTimelineEvent:
    """
    Build a timeline event for a deal stage change.

    This is a pure function that creates a DealTimelineEvent object.
    Called by DealSyncService for inline creation when stage history is recorded.

    Args:
        organisation_id: Org UUID
        deal_id: Deal UUID
        history_entry: Dictionary containing stage history data with keys:
            - external_id or history_id: Unique identifier
            - stage_id: Internal UUID for new stage (preferred)
            - from_stage_id: Internal UUID for previous stage (preferred)
            - stage_external_id: Provider's stage ID (fallback)
            - from_stage_external_id: Provider's previous stage ID (fallback)
            - entered_at: Timestamp when stage was entered
            - stage_label: Display label for new stage
            - from_stage_label: Display label for previous stage
        event_source: Source CRM system (CRM_HUBSPOT, CRM_SALESFORCE)
        deal_context: Optional deal context for frontend display:
            - deal_name, company_name, amount, currency, pipeline_name

    Returns:
        DealTimelineEvent (not yet added to session)
    """
    entered_at = history_entry.get("entered_at") or datetime.now(UTC)

    # Get display labels - these should be resolved by the caller
    old_stage_label = history_entry.get("from_stage_label")
    new_stage_label = history_entry.get("stage_label")

    # Get internal stage IDs (UUIDs) - preferred for storage
    old_stage_id = history_entry.get("from_stage_id")
    new_stage_id = history_entry.get("stage_id")

    # Fallback to external IDs if internal not available
    old_stage_external = history_entry.get("from_stage_external_id")
    new_stage_external = history_entry.get("stage_external_id")

    # Use labels for display, fall back to external IDs if labels not resolved
    old_display = old_stage_label or old_stage_external
    new_display = new_stage_label or new_stage_external

    # Build title with human-readable labels
    title = f"Stage: {old_display} → {new_display}" if old_display else f"Stage: {new_display}"

    # Build source_record_id from history_id or external_id
    source_id = history_entry.get("history_id") or history_entry.get("external_id")

    resolved_source = event_source or TimelineEventSource.CRM_HUBSPOT

    # Build metadata with internal IDs (preferred) and display labels
    metadata: dict[str, Any] = {
        # Internal stage IDs (UUIDs) - use these for lookups
        "old_stage_id": old_stage_id,
        "new_stage_id": new_stage_id,
        # External IDs for reference/debugging
        "old_stage_external_id": old_stage_external,
        "new_stage_external_id": new_stage_external,
        # Display labels for frontend
        "old_stage_label": old_stage_label,
        "new_stage_label": new_stage_label,
    }

    # Add deal context if provided (for frontend convenience)
    if deal_context:
        metadata["deal_name"] = deal_context.get("deal_name")
        metadata["company_name"] = deal_context.get("company_name")
        metadata["pipeline_name"] = deal_context.get("pipeline_name")
        metadata["currency"] = deal_context.get("currency")
        if deal_context.get("amount") is not None:
            metadata["amount"] = float(deal_context["amount"])

    return DealTimelineEvent(
        organisation_id=organisation_id,
        deal_id=deal_id,
        event_type=TimelineEventType.STAGE_CHANGE,
        event_source=resolved_source,
        event_timestamp=entered_at,
        source_record_id=str(source_id) if source_id else str(deal_id),
        title=title[:500],
        subtitle=None,
        direction=None,
        status=None,
        participant_names=[],
        participant_count=0,
        event_metadata=metadata,
    )


def build_deal_created_timeline_event(
    organisation_id: UUID,
    deal: CRMDeal,
    event_source: TimelineEventSource | None = None,
) -> DealTimelineEvent:
    """
    Build a timeline event for deal creation.

    This is a pure function that creates a DealTimelineEvent object.
    Called by DealSyncService for inline creation when a new deal is synced.

    Args:
        organisation_id: Org UUID
        deal: The newly created CRMDeal record
        event_source: Source CRM system (CRM_HUBSPOT, CRM_SALESFORCE)

    Returns:
        DealTimelineEvent (not yet added to session)
    """
    # Use source_created_at if available (from CRM), otherwise use created_at
    created_at = deal.source_created_at or deal.created_at

    # Build title
    title = f"Deal Created: {deal.name}" if deal.name else "Deal Created"

    # Build subtitle with initial stage if available
    subtitle = None
    if deal.stage_name:
        subtitle = f"Initial Stage: {deal.stage_name}"

    resolved_source = event_source or TimelineEventSource.CRM_HUBSPOT

    # Build metadata with deal and company information
    metadata: dict[str, Any] = {
        # Deal information
        "deal_name": deal.name,
        "initial_stage": deal.stage_name,
        "pipeline_name": deal.pipeline_name,
        "currency": deal.currency,
        # Company information
        "company_name": deal.company_name,
        "company_domain": deal.company_domain,
        # Owner information
        "owner_name": deal.owner_name,
        # Primary contact
        "primary_contact_name": deal.primary_contact_name,
        "primary_contact_email": deal.primary_contact_email,
    }
    if deal.owner_id:
        metadata["owner_id"] = str(deal.owner_id)
    if deal.amount is not None:
        metadata["amount"] = float(deal.amount)

    return DealTimelineEvent(
        organisation_id=organisation_id,
        deal_id=deal.deal_id,
        event_type=TimelineEventType.DEAL_CREATED,
        event_source=resolved_source,
        event_timestamp=created_at,
        source_record_id=str(deal.deal_id),
        title=title[:500],
        subtitle=subtitle[:500] if subtitle else None,
        direction=None,
        status=None,
        participant_names=[],
        participant_count=0,
        event_metadata=metadata,
    )
