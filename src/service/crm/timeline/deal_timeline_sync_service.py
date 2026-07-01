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
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.schema import Table
from src.domain.models.organisation_tables.sync.calendar import (
    CalendarEvent,
    CalendarEventDealAssociation,
)
from src.domain.models.organisation_tables.sync.crm.contacts import CRMContact
from src.domain.models.organisation_tables.sync.crm.deals import (
    CRMDeal,
    CRMDealParticipant,
    CRMDealStage,
    CRMDealStageHistory,
    CRMParticipantType,
)
from src.domain.models.organisation_tables.sync.crm.stakeholders import (
    ApprovalStatus,
)
from src.domain.models.organisation_tables.sync.email_models import (
    DealEmailAssociation,
    Email,
)
from src.domain.models.organisation_tables.sync.integration_models import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.risk_blocker_models import (
    DealRiskBlocker,
    RiskBlockerStatus,
    RiskBlockerType,
)
from src.domain.models.organisation_tables.sync.timeline_models import (
    DealTimelineEvent,
    TimelineEventSource,
    TimelineEventType,
)
from src.infra.database.sessions import acquire_org_async_session
from src.infra.utils.logger import get_logger
from src.service.crm.meetings.attendee_display import resolve_attendee_display_name
from src.service.crm.participants.stakeholder_adapter import (
    participant_to_stakeholder_detail_dict,
)
from src.service.crm.timeline.timeline_event_source_resolver import (
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
    """Rebuild deal timeline cards from emails, meetings, CRM stages, and risks.

    Each sub-operation acquires its own session via acquire_org_async_session
    and releases it immediately, preventing connection drops from long-held
    transactions.

    Use this service for:
    - Backfilling timeline events from existing associations
    - Syncing timeline for a single deal
    - Org-wide timeline reconciliation

    For real-time creation during association matching, use the builder
    methods directly in DealEmailMatcher/DealMeetingMatcher.
    """

    def __init__(self, organisation_id: UUID):
        self.organisation_id = organisation_id
        self._stage_lookup_cache: dict[str, dict[str, Any]] | None = None

    async def _get_stage_lookup(self, session: AsyncSession) -> dict[str, dict[str, Any]]:
        """Build a lookup map of local stage_id -> stage info.

        Args:
            session: Database session to use

        Returns dict mapping stage_id to:
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
        result = await session.execute(stmt)
        stages = result.scalars().all()

        self._stage_lookup_cache = {
            str(stage.stage_id): {
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
        stage_id: UUID | str | None,
    ) -> tuple[str | None, str | None]:
        """Resolve local stage_id to (internal_stage_id, display_label).

        Returns:
            Tuple of (stage_id as string, display_label)
        """
        if not stage_id:
            return None, None

        stage_key = str(stage_id)
        stage_info = stage_lookup.get(stage_key)
        if stage_info:
            display_label = stage_info.get("label") or stage_info.get("name")
            return str(stage_info["stage_id"]), display_label

        return stage_key, None

    # -------------------------------------------------------------------------
    # Batch Sync Methods
    # -------------------------------------------------------------------------

    async def sync_deal_timeline(
        self,
        deal_id: UUID,
        event_types: list[TimelineEventType] | None = None,
    ) -> dict[str, int]:
        """Sync all timeline events for a single deal.

        Each sub-operation acquires a fresh session via acquire_org_async_session.
        Idempotent — uses unique constraint to avoid duplicates.

        Args:
            deal_id: Deal UUID
            event_types: Optional filter - only sync specific event types

        Returns:
            Stats dict with counts for each event type synced
        """
        results = await self._sync_deal_timeline_internal(deal_id, event_types)

        logger.info(
            f"Timeline sync for deal {deal_id}: "
            f"created={results['total_created']}, skipped={results['total_skipped']}"
        )
        return results

    async def _sync_deal_timeline_internal(
        self,
        deal_id: UUID,
        event_types: list[TimelineEventType] | None = None,
    ) -> dict[str, int]:
        """Internal method to sync timeline events for a single deal.

        Each sub-operation (emails, meetings, stages, etc.) acquires and
        releases its own session so no single connection is held across
        the full deal sync.

        Args:
            deal_id: Deal UUID
            event_types: Optional filter - only sync specific event types

        Returns:
            Stats dict with counts
        """
        results = {
            "emails_synced": 0,
            "meetings_synced": 0,
            "stage_changes_synced": 0,
            "deal_created_synced": 0,
            "risks_blockers_synced": 0,
            "stakeholders_synced": 0,
            "total_created": 0,
            "total_skipped": 0,
        }

        # Fetch existing timeline events for this deal (for dedup)
        async for session in acquire_org_async_session(self.organisation_id):
            existing_keys = await self._get_existing_event_keys(session, deal_id)

        if not event_types or TimelineEventType.EMAIL in event_types:
            async for session in acquire_org_async_session(self.organisation_id):
                email_stats = await self._sync_email_timeline_events(
                    session, deal_id, existing_keys
                )
                await session.commit()
            results["emails_synced"] = email_stats["synced"]
            results["total_created"] += email_stats["created"]
            results["total_skipped"] += email_stats["skipped"]

        if not event_types or TimelineEventType.MEETING in event_types:
            async for session in acquire_org_async_session(self.organisation_id):
                meeting_stats = await self._sync_meeting_timeline_events(
                    session, deal_id, existing_keys
                )
                await session.commit()
            results["meetings_synced"] = meeting_stats["synced"]
            results["total_created"] += meeting_stats["created"]
            results["total_skipped"] += meeting_stats["skipped"]

        if not event_types or TimelineEventType.STAGE_CHANGE in event_types:
            async for session in acquire_org_async_session(self.organisation_id):
                stage_stats = await self._sync_stage_change_timeline_events(
                    session, deal_id, existing_keys
                )
                await session.commit()
            results["stage_changes_synced"] = stage_stats["synced"]
            results["total_created"] += stage_stats["created"]
            results["total_skipped"] += stage_stats["skipped"]

        if not event_types or TimelineEventType.DEAL_CREATED in event_types:
            async for session in acquire_org_async_session(self.organisation_id):
                created_stats = await self._sync_deal_created_timeline_event(
                    session, deal_id, existing_keys
                )
                await session.commit()
            results["deal_created_synced"] = created_stats["synced"]
            results["total_created"] += created_stats["created"]
            results["total_skipped"] += created_stats["skipped"]

        if not event_types or any(
            t in event_types
            for t in [
                TimelineEventType.BLOCKER_ADDED,
                TimelineEventType.BLOCKER_RESOLVED,
                TimelineEventType.BLOCKER_DISMISSED,
                TimelineEventType.RISK_FLAGGED,
            ]
        ):
            async for session in acquire_org_async_session(self.organisation_id):
                rb_stats = await self._sync_risk_blocker_timeline_events(
                    session, deal_id, existing_keys
                )
                await session.commit()
            results["risks_blockers_synced"] = rb_stats["synced"]
            results["total_created"] += rb_stats["created"]
            results["total_skipped"] += rb_stats["skipped"]

        if not event_types or any(
            t in event_types
            for t in [
                TimelineEventType.STAKEHOLDER_ADDED,
                TimelineEventType.STAKEHOLDER_REMOVED,
            ]
        ):
            async for session in acquire_org_async_session(self.organisation_id):
                stakeholder_stats = await self._sync_stakeholder_timeline_events(
                    session, deal_id, existing_keys
                )
                await session.commit()
            results["stakeholders_synced"] = stakeholder_stats["synced"]
            results["total_created"] += stakeholder_stats["created"]
            results["total_skipped"] += stakeholder_stats["skipped"]

        return results

    async def sync_organisation_timeline(
        self,
        deal_ids: list[UUID] | None = None,
        event_types: list[TimelineEventType] | None = None,
        batch_size: int = 50,
    ) -> dict[str, int]:
        """
        Sync timeline events for all deals in the organisation.

        Uses fresh session pattern: acquires a new session per deal to avoid
        connection drops from long-held transactions.

        Args:
            deal_ids: Optional list of specific deal IDs to sync
            event_types: Optional filter - only sync specific event types
            batch_size: Log progress every N deals

        Returns:
            Aggregate stats dict
        """
        results = {
            "deals_processed": 0,
            "emails_synced": 0,
            "meetings_synced": 0,
            "stage_changes_synced": 0,
            "deal_created_synced": 0,
            "risks_blockers_synced": 0,
            "stakeholders_synced": 0,
            "total_created": 0,
            "total_skipped": 0,
        }

        # Phase 1: Get deal IDs to process (fresh session)
        ids_to_process: list[UUID] = []
        if deal_ids:
            ids_to_process = deal_ids
        else:
            async for session in acquire_org_async_session(self.organisation_id):
                deal_table: Table = _table(CRMDeal)
                stmt = select(deal_table.c.deal_id).where(
                    deal_table.c.organisation_id == self.organisation_id,
                    deal_table.c.is_deleted.is_(False),
                )
                result = await session.execute(stmt)
                ids_to_process = list(result.scalars().all())

        if not ids_to_process:
            logger.info(f"No deals to sync timeline for org {self.organisation_id}")
            return results

        logger.info(
            f"Syncing timeline for {len(ids_to_process)} deals in org {self.organisation_id}"
        )

        # Phase 2: Process each deal. _sync_deal_timeline_internal acquires
        # fresh sessions per sub-operation when called without a session,
        # preventing connection drops from long-held transactions.
        for idx, deal_id in enumerate(ids_to_process):
            try:
                self._stage_lookup_cache = None

                deal_stats = await self._sync_deal_timeline_internal(deal_id, event_types)

                results["deals_processed"] += 1
                results["emails_synced"] += deal_stats["emails_synced"]
                results["meetings_synced"] += deal_stats["meetings_synced"]
                results["stage_changes_synced"] += deal_stats["stage_changes_synced"]
                results["deal_created_synced"] += deal_stats["deal_created_synced"]
                results["risks_blockers_synced"] += deal_stats.get("risks_blockers_synced", 0)
                results["stakeholders_synced"] += deal_stats.get("stakeholders_synced", 0)
                results["total_created"] += deal_stats["total_created"]
                results["total_skipped"] += deal_stats["total_skipped"]
            except Exception:
                logger.error(
                    "Timeline sync failed for deal %s in org %s, skipping",
                    deal_id,
                    self.organisation_id,
                    exc_info=True,
                )

            if (idx + 1) % batch_size == 0:
                logger.debug(
                    "Timeline sync progress: %d/%d deals",
                    idx + 1,
                    len(ids_to_process),
                )

        logger.info(
            f"Organisation timeline sync complete: {results['deals_processed']} deals, "
            f"{results['total_created']} events created"
        )
        return results

    # -------------------------------------------------------------------------
    # Private Helpers
    # -------------------------------------------------------------------------

    async def _get_existing_event_keys(
        self, session: AsyncSession, deal_id: UUID
    ) -> set[tuple[str, str]]:
        """Get existing (event_type, source_record_id) pairs for a deal.

        Args:
            session: Database session to use
            deal_id: Deal UUID
        """
        t: Table = _table(DealTimelineEvent)
        stmt = select(
            t.c.event_type,
            t.c.source_record_id,
        ).where(
            t.c.organisation_id == self.organisation_id,
            t.c.deal_id == deal_id,
        )
        result = await session.execute(stmt)
        # Normalise event_type to a string so callers can safely compare keys
        # regardless of whether SQLAlchemy materialises it as an Enum or str.
        keys: set[tuple[str, str]] = set()
        for event_type, source_record_id in result.all():
            event_type_str = _enum_value(event_type) or str(event_type)
            keys.add((event_type_str, source_record_id))
        return keys

    async def _insert_timeline_event_ignore_conflict(
        self, session: AsyncSession, event: DealTimelineEvent
    ) -> bool:
        """Insert a timeline event, or update metadata if it already exists.

        Uses upsert pattern to ensure existing events get updated metadata
        (e.g., calendar_event_id, email_id) on re-sync.

        Args:
            session: Database session to use
            event: Timeline event to insert or update

        Returns:
            True if inserted or updated, False if no changes.
        """
        values: dict[str, Any] = event.model_dump()  # SQLModel / Pydantic v2

        # Ensure enums are stored as strings
        values["event_type"] = _enum_value(values.get("event_type"))
        values["event_source"] = _enum_value(values.get("event_source"))

        # Fields to update on conflict (metadata and timestamps)
        update_fields = {
            "event_metadata": values.get("event_metadata"),
            "title": values.get("title"),
            "subtitle": values.get("subtitle"),
            "participant_count": values.get("participant_count"),
            "updated_at": datetime.now(UTC),
        }

        stmt = (
            insert(DealTimelineEvent)
            .values(**values)
            .on_conflict_do_update(
                constraint="uq_timeline_event_source",
                set_=update_fields,
            )
        )
        result = await session.execute(stmt)
        return (getattr(result, "rowcount", 0) or 0) > 0

    async def _sync_email_timeline_events(
        self,
        session: AsyncSession,
        deal_id: UUID,
        existing_keys: set[tuple[str, str]],
    ) -> dict[str, int]:
        """Batch sync email associations → timeline events.

        Args:
            session: Database session to use
            deal_id: Deal UUID
            existing_keys: Set of existing (event_type, source_record_id) pairs
        """
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
        result = await session.execute(stmt)

        for assoc, email, integration in result:
            stats["synced"] += 1
            key = (TimelineEventType.EMAIL.value, str(assoc.association_id))

            if key in existing_keys:
                stats["skipped"] += 1
                continue

            # Resolve the provider source before building the row so replayed
            # backfills keep the same source labels as real-time matchers.
            integration_type = getattr(integration, "integration_type", None)
            resolved_source = resolve_email_event_source(integration_type)
            timeline_event = build_email_timeline_event(
                organisation_id=self.organisation_id,
                deal_id=deal_id,
                association=assoc,
                email=email,
                event_source=resolved_source,
            )
            created = await self._insert_timeline_event_ignore_conflict(session, timeline_event)
            if created:
                stats["created"] += 1
                existing_keys.add(key)
            else:
                stats["skipped"] += 1
                existing_keys.add(key)

        return stats

    async def _sync_meeting_timeline_events(
        self,
        session: AsyncSession,
        deal_id: UUID,
        existing_keys: set[tuple[str, str]],
    ) -> dict[str, int]:
        """Batch sync meeting associations → timeline events.

        Args:
            session: Database session to use
            deal_id: Deal UUID
            existing_keys: Set of existing (event_type, source_record_id) pairs
        """
        stats = {"synced": 0, "created": 0, "skipped": 0}

        assoc_table: Table = _table(CalendarEventDealAssociation)
        event_table: Table = _table(CalendarEvent)
        integration_table: Table = _table(KnowledgeIntegration)

        # Fetch all meeting associations for this deal with event data.
        # Use selectinload for attendees/user_states to avoid lazy-load issues in async context.
        attendees_attr = cast(Any, CalendarEvent.attendees)
        user_states_attr = cast(Any, CalendarEvent.user_states)
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
        result = await session.execute(stmt)
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
            it_result = await session.execute(it_stmt)
            integration_type_by_id = {
                row.integration_id: row.integration_type for row in it_result.scalars().all()
            }

        for assoc, event in rows:
            stats["synced"] += 1
            key = (TimelineEventType.MEETING.value, str(assoc.association_id))

            if key in existing_keys:
                stats["skipped"] += 1
                continue

            # Meeting source can come from the event or attendee user state;
            # resolve it here so the pure builder stays storage-only.
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
            created = await self._insert_timeline_event_ignore_conflict(session, timeline_event)
            if created:
                stats["created"] += 1
                existing_keys.add(key)
            else:
                stats["skipped"] += 1
                existing_keys.add(key)

        return stats

    async def _sync_stage_change_timeline_events(
        self,
        session: AsyncSession,
        deal_id: UUID,
        existing_keys: set[tuple[str, str]],
    ) -> dict[str, int]:
        """Batch sync stage history records → timeline events.

        Args:
            session: Database session to use
            deal_id: Deal UUID
            existing_keys: Set of existing (event_type, source_record_id) pairs
        """
        stats = {"synced": 0, "created": 0, "skipped": 0}

        # Build the lookup once per deal; every history row needs the same
        # provider-local stage labels for readable timeline cards.
        stage_lookup = await self._get_stage_lookup(session)

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
        result = await session.execute(stmt)

        for history, integration in result:
            stats["synced"] += 1
            key = (TimelineEventType.STAGE_CHANGE.value, str(history.history_id))

            if key in existing_keys:
                stats["skipped"] += 1
                continue

            # Resolve stage names/labels from lookup
            new_stage_id, new_stage_label = self._resolve_stage_label(
                stage_lookup, history.stage_id
            )
            old_stage_id, old_stage_label = self._resolve_stage_label(
                stage_lookup, history.from_stage_id
            )

            # Build history entry dict for builder function with resolved labels
            history_entry = {
                "history_id": str(history.history_id),
                "deal_id": history.deal_id,
                "stage_id": new_stage_id,
                "from_stage_id": old_stage_id,
                "entered_at": history.entered_at,
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
            created = await self._insert_timeline_event_ignore_conflict(session, timeline_event)
            if created:
                stats["created"] += 1
                existing_keys.add(key)
            else:
                stats["skipped"] += 1
                existing_keys.add(key)

        return stats

    async def _sync_deal_created_timeline_event(
        self,
        session: AsyncSession,
        deal_id: UUID,
        existing_keys: set[tuple[str, str]],
    ) -> dict[str, int]:
        """Create DEAL_CREATED timeline event for an existing deal.

        Args:
            session: Database session to use
            deal_id: Deal UUID
            existing_keys: Set of existing (event_type, source_record_id) pairs
        """
        stats = {"synced": 0, "created": 0, "skipped": 0}

        # Deal-created has no separate source row, so the deal_id itself is the
        # replay key for idempotent backfills.
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
        result = await session.execute(stmt)
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
        created = await self._insert_timeline_event_ignore_conflict(session, timeline_event)
        if created:
            stats["created"] = 1
            existing_keys.add(key)
        else:
            stats["skipped"] = 1
            existing_keys.add(key)

        return stats

    async def _sync_risk_blocker_timeline_events(
        self,
        session: AsyncSession,
        deal_id: UUID,
        existing_keys: set[tuple[str, str]],
    ) -> dict[str, int]:
        """Batch sync risk/blocker records → timeline events.

        Args:
            session: Database session to use
            deal_id: Deal UUID
            existing_keys: Set of existing (event_type, source_record_id) pairs
        """
        stats = {"synced": 0, "created": 0, "skipped": 0}

        risk_blocker_table: Table = _table(DealRiskBlocker)

        # Fetch all active and resolved risk/blockers (skip dismissed/won't-fix)
        stmt = select(DealRiskBlocker).where(
            risk_blocker_table.c.organisation_id == self.organisation_id,
            risk_blocker_table.c.deal_id == deal_id,
            risk_blocker_table.c.status.in_(
                [
                    RiskBlockerStatus.OPEN.value,
                    RiskBlockerStatus.IN_PROGRESS.value,
                    RiskBlockerStatus.RESOLVED.value,
                    RiskBlockerStatus.DISMISSED.value,
                    RiskBlockerStatus.WONT_FIX.value,
                ]
            ),
        )
        result = await session.execute(stmt)

        for risk_blocker in result.scalars().all():
            stats["synced"] += 1

            # Determine event type based on item_type and status
            item_type_val = (
                risk_blocker.item_type.value
                if hasattr(risk_blocker.item_type, "value")
                else str(risk_blocker.item_type)
            )
            status_val = (
                risk_blocker.status.value
                if hasattr(risk_blocker.status, "value")
                else str(risk_blocker.status)
            )

            # Open risks/blockers need an initial lifecycle card before later
            # status transitions can be appended chronologically.
            if item_type_val == RiskBlockerType.RISK.value:
                initial_event_type = TimelineEventType.RISK_FLAGGED
            else:
                initial_event_type = TimelineEventType.BLOCKER_ADDED

            key = (initial_event_type.value, str(risk_blocker.risk_blocker_id))

            if key not in existing_keys:
                timeline_event = build_risk_blocker_timeline_event(
                    organisation_id=self.organisation_id,
                    deal_id=deal_id,
                    risk_blocker=risk_blocker,
                    event_type=initial_event_type,
                )
                created = await self._insert_timeline_event_ignore_conflict(session, timeline_event)
                if created:
                    stats["created"] += 1
                    existing_keys.add(key)
                else:
                    stats["skipped"] += 1
                    existing_keys.add(key)
            else:
                stats["skipped"] += 1

            # If resolved, also create a RESOLVED event
            if status_val == RiskBlockerStatus.RESOLVED.value and risk_blocker.closed_at:
                resolved_key = (
                    TimelineEventType.BLOCKER_RESOLVED.value,
                    str(risk_blocker.risk_blocker_id),
                )
                if resolved_key not in existing_keys:
                    resolved_event = build_risk_blocker_timeline_event(
                        organisation_id=self.organisation_id,
                        deal_id=deal_id,
                        risk_blocker=risk_blocker,
                        event_type=TimelineEventType.BLOCKER_RESOLVED,
                    )
                    created = await self._insert_timeline_event_ignore_conflict(
                        session, resolved_event
                    )
                    if created:
                        stats["created"] += 1
                        existing_keys.add(resolved_key)

            # If dismissed, also create a DISMISSED event
            if status_val == RiskBlockerStatus.DISMISSED.value:
                dismissed_key = (
                    TimelineEventType.BLOCKER_DISMISSED.value,
                    str(risk_blocker.risk_blocker_id),
                )
                if dismissed_key not in existing_keys:
                    dismissed_event = build_risk_blocker_timeline_event(
                        organisation_id=self.organisation_id,
                        deal_id=deal_id,
                        risk_blocker=risk_blocker,
                        event_type=TimelineEventType.BLOCKER_DISMISSED,
                    )
                    created = await self._insert_timeline_event_ignore_conflict(
                        session, dismissed_event
                    )
                    if created:
                        stats["created"] += 1
                        existing_keys.add(dismissed_key)

        return stats

    async def _sync_stakeholder_timeline_events(
        self,
        session: AsyncSession,
        deal_id: UUID,
        existing_keys: set[tuple[str, str]],
    ) -> dict[str, int]:
        """Batch sync stakeholder records → timeline events.

        Args:
            session: Database session to use
            deal_id: Deal UUID
            existing_keys: Set of existing (event_type, source_record_id) pairs
        """
        stats = {"synced": 0, "created": 0, "skipped": 0}

        participant_table: Table = _table(CRMDealParticipant)

        stmt = select(CRMDealParticipant).where(
            participant_table.c.organisation_id == self.organisation_id,
            participant_table.c.deal_id == deal_id,
            participant_table.c.participant_type == CRMParticipantType.CONTACT.value,
            participant_table.c.is_deleted.is_(False),
            participant_table.c.approval_status == ApprovalStatus.APPROVED.value,
        )
        result = await session.execute(stmt)

        for stakeholder in result.scalars().all():
            stats["synced"] += 1
            key = (TimelineEventType.STAKEHOLDER_ADDED.value, str(stakeholder.participant_id))

            if key in existing_keys:
                stats["skipped"] += 1
                continue

            timeline_event = build_stakeholder_timeline_event(
                organisation_id=self.organisation_id,
                deal_id=deal_id,
                stakeholder=stakeholder,
                event_type=TimelineEventType.STAKEHOLDER_ADDED,
            )
            created = await self._insert_timeline_event_ignore_conflict(session, timeline_event)
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
        "from_email": sender_email,
    }
    if email:
        # Source ID for API calls
        metadata["email_id"] = str(email.email_id)
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
        organiser_display = resolve_attendee_display_name(
            event.organiser_name, event.organiser_email
        )
        if organiser_display:
            participant_names.append(organiser_display)
        elif event.organiser_email:
            participant_names.append(event.organiser_email)

        # Add attendee names if available (from attendees relationship)
        attendees = getattr(event, "attendees", None)
        if attendees:
            for att in attendees[:3]:
                name = resolve_attendee_display_name(
                    getattr(att, "display_name", None), getattr(att, "email", None)
                ) or getattr(att, "email", None)
                if name and name not in participant_names:
                    participant_names.append(name)

    # Build metadata
    metadata: dict[str, Any] = {
        # Source ID for API calls (e.g., summary generation)
        "calendar_event_id": str(association.event_id),
    }
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
            - history_id: Unique identifier
            - stage_id: Internal UUID for new stage (preferred)
            - from_stage_id: Internal UUID for previous stage (preferred)
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

    # Stage labels are resolved by the sync step so this pure builder can also
    # be used by inline writers that already have label context.
    old_stage_label = history_entry.get("from_stage_label")
    new_stage_label = history_entry.get("stage_label")

    # Keep local stage UUIDs in metadata even when labels are available; detail
    # views use them to navigate back to CRM stage records.
    old_stage_id = history_entry.get("from_stage_id")
    new_stage_id = history_entry.get("stage_id")

    # Use labels for display, fall back to local IDs if labels are not resolved.
    old_display = old_stage_label or old_stage_id
    new_display = new_stage_label or new_stage_id

    # Build title with human-readable labels
    title = f"Stage: {old_display} → {new_display}" if old_display else f"Stage: {new_display}"

    source_id = history_entry.get("history_id")

    resolved_source = event_source or TimelineEventSource.CRM_HUBSPOT

    # Build metadata with internal IDs (preferred) and display labels
    metadata: dict[str, Any] = {
        # Internal stage IDs (UUIDs) - use these for lookups
        "old_stage_id": old_stage_id,
        "new_stage_id": new_stage_id,
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


def build_risk_blocker_timeline_event(
    organisation_id: UUID,
    deal_id: UUID,
    risk_blocker: DealRiskBlocker,
    event_type: TimelineEventType,
    event_source: TimelineEventSource = TimelineEventSource.SYSTEM,
) -> DealTimelineEvent:
    """
    Build a timeline event for a risk/blocker lifecycle change.

    This is a pure function that creates a DealTimelineEvent object.
    Called by RiskBlockerService for inline creation and by sync service for batch.

    Args:
        organisation_id: Org UUID
        deal_id: Deal UUID
        risk_blocker: The DealRiskBlocker record
        event_type: One of BLOCKER_ADDED, BLOCKER_RESOLVED, BLOCKER_DISMISSED, RISK_FLAGGED
        event_source: Source system (defaults to SYSTEM)

    Returns:
        DealTimelineEvent (not yet added to session)
    """
    # Determine title based on event type
    item_type_str = (
        risk_blocker.item_type.value
        if hasattr(risk_blocker.item_type, "value")
        else str(risk_blocker.item_type)
    )

    if event_type == TimelineEventType.BLOCKER_ADDED:
        title = f"Blocker Opened: {risk_blocker.title}"
    elif event_type == TimelineEventType.RISK_FLAGGED:
        title = f"Risk Opened: {risk_blocker.title}"
    elif event_type == TimelineEventType.BLOCKER_RESOLVED:
        prefix = "Risk" if item_type_str == RiskBlockerType.RISK.value else "Blocker"
        title = f"{prefix} Resolved: {risk_blocker.title}"
    elif event_type == TimelineEventType.BLOCKER_DISMISSED:
        prefix = "Risk" if item_type_str == RiskBlockerType.RISK.value else "Blocker"
        title = f"{prefix} Dismissed: {risk_blocker.title}"
    else:
        title = f"{item_type_str}: {risk_blocker.title}"

    # Build subtitle
    severity_str = (
        risk_blocker.severity.value
        if hasattr(risk_blocker.severity, "value")
        else str(risk_blocker.severity)
    )
    subtitle = f"{item_type_str} - {severity_str}"

    # Determine event timestamp — use closure time for resolve/dismiss events
    if (
        event_type == TimelineEventType.BLOCKER_RESOLVED
        and risk_blocker.closed_at
        or event_type == TimelineEventType.BLOCKER_DISMISSED
        and risk_blocker.closed_at
    ):
        event_timestamp = risk_blocker.closed_at
    else:
        event_timestamp = risk_blocker.identified_at or risk_blocker.created_at

    # Build metadata
    metadata: dict[str, Any] = {
        "risk_blocker_id": str(risk_blocker.risk_blocker_id),
        "item_type": item_type_str,
        "severity": severity_str,
        "category": _enum_value(risk_blocker.category),
        "status": _enum_value(risk_blocker.status),
        "source_type": _enum_value(risk_blocker.source_type),
        "is_ai_generated": risk_blocker.is_ai_generated,
    }

    # Add resolution notes for resolved events
    if event_type == TimelineEventType.BLOCKER_RESOLVED and risk_blocker.resolution_notes:
        metadata["resolution_notes"] = risk_blocker.resolution_notes

    return DealTimelineEvent(
        organisation_id=organisation_id,
        deal_id=deal_id,
        event_type=event_type,
        event_source=event_source,
        event_timestamp=event_timestamp,
        source_record_id=str(risk_blocker.risk_blocker_id),
        title=title[:500],
        subtitle=subtitle[:500] if subtitle else None,
        direction=None,
        status=_enum_value(risk_blocker.status),
        participant_names=[],
        participant_count=0,
        event_metadata=metadata,
    )


def build_stakeholder_timeline_event(
    organisation_id: UUID,
    deal_id: UUID,
    stakeholder: CRMDealParticipant,
    *,
    contact: CRMContact | None = None,
    event_type: TimelineEventType = TimelineEventType.STAKEHOLDER_ADDED,
    event_source: TimelineEventSource = TimelineEventSource.SYSTEM,
) -> DealTimelineEvent:
    """
    Build a timeline event for a stakeholder lifecycle change.

    This is a pure function that creates a DealTimelineEvent object.
    Called by StakeholdersService for inline creation and by sync service for batch.

    Args:
        organisation_id: Org UUID
        deal_id: Deal UUID
        stakeholder: The CRMDealParticipant contact participant record
        event_type: One of STAKEHOLDER_ADDED, STAKEHOLDER_REMOVED
        event_source: Source system (defaults to SYSTEM)

    Returns:
        DealTimelineEvent (not yet added to session)
    """
    profile = participant_to_stakeholder_detail_dict(stakeholder, contact=contact)
    display_name = profile["name"] or profile["email"] or "Unknown Stakeholder"

    # Determine title based on event type
    if event_type == TimelineEventType.STAKEHOLDER_ADDED:
        title = f"Stakeholder Added: {display_name}"
    elif event_type == TimelineEventType.STAKEHOLDER_REMOVED:
        title = f"Stakeholder Removed: {display_name}"
    else:
        title = f"Stakeholder: {display_name}"

    # Build subtitle
    source_str = _enum_value(stakeholder.participant_source) or str(stakeholder.participant_source)
    subtitle = profile["job_title"] or f"{source_str} discovery"

    # Determine event timestamp
    if event_type == TimelineEventType.STAKEHOLDER_REMOVED and stakeholder.updated_at:
        event_timestamp = stakeholder.updated_at
    else:
        event_timestamp = stakeholder.created_at

    # Build metadata
    metadata: dict[str, Any] = {
        "stakeholder_id": str(stakeholder.participant_id),
        "participant_id": str(stakeholder.participant_id),
        "email": profile["email"],
        "name": profile["name"],
        "job_title": profile["job_title"],
        "source": source_str,
        "stance": _enum_value(stakeholder.stance),
        "decision_power": _enum_value(stakeholder.decision_power),
    }

    return DealTimelineEvent(
        organisation_id=organisation_id,
        deal_id=deal_id,
        event_type=event_type,
        event_source=event_source,
        event_timestamp=event_timestamp,
        source_record_id=str(stakeholder.participant_id),
        title=title[:500],
        subtitle=subtitle[:500] if subtitle else None,
        direction=None,
        status=_enum_value(stakeholder.approval_status),
        participant_names=[profile["name"]] if profile["name"] else [],
        participant_count=1,
        event_metadata=metadata,
    )
