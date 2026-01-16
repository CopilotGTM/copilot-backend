"""Deal timeline service for managing and querying deal timeline events."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import QueryableAttribute, selectinload
from sqlalchemy.sql.schema import Table
from src.domain.models.organisation_tables.sync.calendar import (
    CalendarEvent,
    CalendarEventDealAssociation,
    CalendarEventUserState,
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
from src.infra.utils.cursor import decode_cursor, encode_cursor
from src.infra.utils.logger import get_logger
from src.service.crm.timeline_event_source_resolver import (
    resolve_email_event_source,
    resolve_meeting_event_source,
)

logger = get_logger("deal_timeline_service")


def _table(model: Any) -> Table:
    """Safely extract the SQLAlchemy table for a mapped model."""
    table_name = getattr(model, "__tablename__", None)
    if table_name is None:
        raise DealTimelineServiceError("Model missing __tablename__ for table lookup")

    table = model.metadata.tables.get(table_name)
    if table is None:
        raise DealTimelineServiceError(f"Table not found for model {model}")

    return cast(Table, table)


def _enum_value(val: Enum | str | None) -> str | None:
    """Safely extract string value from an enum or pass through string.

    Depending on model configuration/loading strategy, SQLModel/SQLAlchemy can
    materialize enum columns as either enum instances or raw strings.
    """
    if val is None:
        return None
    if isinstance(val, str):
        return val
    return str(val.value)


class DealTimelineServiceError(Exception):
    """Custom exception for deal timeline service errors."""

    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class DealTimelineService:
    """Service for managing deal timeline events."""

    def __init__(self, session: AsyncSession, organisation_id: UUID):
        """Initialize the service with an org-scoped session.

        Args:
            session: Async database session scoped to the organisation
            organisation_id: UUID of the organisation to scope queries
        """
        self.session = session
        self.organisation_id = organisation_id

    async def close(self) -> None:
        """Dispose the underlying session when the service is released."""
        try:
            await self.session.close()
        except Exception:
            logger.debug("DealTimelineService close exception", exc_info=True)

    # -------------------------------------------------------------------------
    # Query Methods (for API)
    # -------------------------------------------------------------------------

    async def get_timeline_events(
        self,
        deal_id: UUID,
        limit: int = 20,
        cursor: str | None = None,
        event_types: list[TimelineEventType] | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        include_excluded: bool = False,
        sort_order: str = "desc",
    ) -> tuple[list[DealTimelineEvent], str | None, int]:
        """
        Get timeline events for a deal with pagination and filtering.

        Args:
            deal_id: UUID of the deal
            limit: Maximum number of events to return
            cursor: Opaque cursor for pagination
            event_types: Optional filter by event type(s)
            start_date: Optional filter events after this date
            end_date: Optional filter events before this date
            include_excluded: Whether to include excluded events
            sort_order: Sort order ('asc' or 'desc')

        Returns:
            Tuple of (events, next_cursor, total_count)
        """
        t: Table = _table(DealTimelineEvent)

        # Base query
        stmt = select(DealTimelineEvent).where(
            t.c.organisation_id == self.organisation_id,
            t.c.deal_id == deal_id,
        )

        # Exclude hidden events by default
        if not include_excluded:
            stmt = stmt.where(t.c.excluded_at.is_(None))

        # Filter by event types
        if event_types:
            type_values = [t.value if hasattr(t, "value") else str(t) for t in event_types]
            stmt = stmt.where(t.c.event_type.in_(type_values))

        # Date range filters
        if start_date:
            stmt = stmt.where(t.c.event_timestamp >= start_date)
        if end_date:
            stmt = stmt.where(t.c.event_timestamp <= end_date)

        # Cursor-based pagination
        if cursor:
            try:
                ts, event_id = decode_cursor(cursor)
            except Exception as e:
                raise DealTimelineServiceError("Invalid cursor", status_code=400) from e

            if sort_order == "desc":
                stmt = stmt.where(
                    or_(
                        t.c.event_timestamp < ts,
                        and_(
                            t.c.event_timestamp == ts,
                            t.c.timeline_event_id < event_id,
                        ),
                    )
                )
            else:
                stmt = stmt.where(
                    or_(
                        t.c.event_timestamp > ts,
                        and_(
                            t.c.event_timestamp == ts,
                            t.c.timeline_event_id > event_id,
                        ),
                    )
                )

        # Apply ordering
        if sort_order == "desc":
            stmt = stmt.order_by(
                t.c.event_timestamp.desc(),
                t.c.timeline_event_id.desc(),
            )
        else:
            stmt = stmt.order_by(
                t.c.event_timestamp.asc(),
                t.c.timeline_event_id.asc(),
            )

        # Fetch one more than limit to determine if there are more results
        stmt = stmt.limit(limit + 1)

        result = await self.session.execute(stmt)
        events = list(result.scalars().all())

        # Determine next cursor
        next_cursor = None
        if len(events) > limit:
            events = events[:limit]
            last_event = events[-1]
            next_cursor = encode_cursor(last_event.event_timestamp, last_event.timeline_event_id)

        # Get total count (separate query for efficiency)
        count_stmt = select(t.c.timeline_event_id).where(
            t.c.organisation_id == self.organisation_id,
            t.c.deal_id == deal_id,
        )
        if not include_excluded:
            count_stmt = count_stmt.where(t.c.excluded_at.is_(None))
        if event_types:
            count_stmt = count_stmt.where(t.c.event_type.in_(type_values))

        count_result = await self.session.execute(
            select(func.count()).select_from(count_stmt.subquery())
        )
        total_count = count_result.scalar() or 0

        return events, next_cursor, total_count

    async def get_timeline_event_by_id(self, timeline_event_id: UUID) -> DealTimelineEvent | None:
        """Get a single timeline event by ID."""
        t: Table = _table(DealTimelineEvent)
        stmt = select(DealTimelineEvent).where(
            t.c.organisation_id == self.organisation_id,
            t.c.timeline_event_id == timeline_event_id,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_timeline_event_detail(self, timeline_event_id: UUID) -> dict[str, Any]:
        """Get a timeline event with enriched event_data for detail view.

        Returns:
            Unified TimelineEvent dict with full event_data populated.
        """
        event = await self.get_timeline_event_by_id(timeline_event_id)
        if not event:
            raise DealTimelineServiceError("Timeline event not found", status_code=404)

        event_data = await self._build_enriched_event_data(event)
        return self._build_timeline_event_dict(event, event_data)

    def _build_timeline_event_dict(
        self, event: DealTimelineEvent, event_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Build the unified TimelineEvent dict."""
        return {
            "id": event.timeline_event_id,
            "deal_id": event.deal_id,
            "event_type": _enum_value(event.event_type),
            "event_source": _enum_value(event.event_source),
            "event_timestamp": event.event_timestamp,
            "event_data": event_data,
            "excluded_at": event.excluded_at,
            "created_at": event.created_at,
            "updated_at": event.updated_at,
        }

    def _build_preview_event_data(self, event: DealTimelineEvent) -> dict[str, Any]:
        """Build preview event_data from the denormalized timeline event (no DB fetch).

        For list view - uses only data stored in the timeline event itself.
        """
        event_type = _enum_value(event.event_type)
        metadata = event.event_metadata or {}

        if event_type == TimelineEventType.EMAIL.value:
            return {
                "subject": event.title,
                "snippet": event.subtitle,
                "from_email": metadata.get("from_email"),
            }

        if event_type == TimelineEventType.MEETING.value:
            return {
                "title": event.title,
                "duration_minutes": metadata.get("duration_minutes"),
                "attendee_count": event.participant_count,
            }

        if event_type == TimelineEventType.STAGE_CHANGE.value:
            return {
                "old_stage": metadata.get("old_stage"),
                "new_stage": metadata.get("new_stage"),
                "old_stage_label": metadata.get("old_stage_label"),
                "new_stage_label": metadata.get("new_stage_label"),
            }

        if event_type == TimelineEventType.DEAL_CREATED.value:
            return {
                "initial_stage": metadata.get("initial_stage"),
                "pipeline_name": metadata.get("pipeline_name"),
                "amount": metadata.get("amount"),
            }

        # Fallback for unknown types
        return {"preview": event.title}

    async def _build_enriched_event_data(self, event: DealTimelineEvent) -> dict[str, Any]:
        """Build enriched event_data by fetching from source tables.

        For detail view - fetches additional data from source records.
        """
        event_type = _enum_value(event.event_type)

        if event_type == TimelineEventType.EMAIL.value:
            return await self._fetch_email_event_data(event)
        if event_type == TimelineEventType.MEETING.value:
            return await self._fetch_meeting_event_data(event)

        # STAGE_CHANGE and DEAL_CREATED don't need enrichment - all data is in metadata
        if event_type in (
            TimelineEventType.STAGE_CHANGE.value,
            TimelineEventType.DEAL_CREATED.value,
        ):
            return self._build_preview_event_data(event)

        # Fallback
        return {"preview": event.title}

    async def _fetch_email_event_data(self, event: DealTimelineEvent) -> dict[str, Any]:
        """Fetch enriched email data for detail view."""
        # Start with preview data
        preview = self._build_preview_event_data(event)

        try:
            association_id = UUID(event.source_record_id)
        except Exception:
            logger.warning("Invalid email source_record_id: %s", event.source_record_id)
            return preview

        association_table: Table = _table(DealEmailAssociation)
        stmt = select(DealEmailAssociation).where(
            association_table.c.organisation_id == self.organisation_id,
            association_table.c.association_id == association_id,
        )
        result = await self.session.execute(stmt)
        association = result.scalar_one_or_none()
        if not association:
            return preview

        # Best-effort email message fetch
        email: Email | None = None
        if association.internet_message_id:
            email_table: Table = _table(Email)
            email_stmt = (
                select(Email)
                .where(
                    email_table.c.organisation_id == self.organisation_id,
                    email_table.c.internet_message_id == association.internet_message_id,
                )
                .limit(1)
            )
            email_result = await self.session.execute(email_stmt)
            email = email_result.scalar_one_or_none()

        return {
            # Preview fields
            "subject": (email.subject if email else None)
            or association.email_subject
            or event.title,
            "snippet": (email.snippet if email else None) or event.subtitle,
            "from_email": email.from_email if email else preview.get("from_email"),
            # Detail enrichment
            "to_emails": email.to_emails if email else None,
            "cc_emails": email.cc_emails if email else None,
            "thread_id": email.thread_id if email else None,
            "has_attachments": email.has_attachments if email else None,
        }

    async def _fetch_meeting_event_data(self, event: DealTimelineEvent) -> dict[str, Any]:
        """Fetch enriched meeting data for detail view."""
        # Start with preview data
        preview = self._build_preview_event_data(event)

        try:
            association_id = UUID(event.source_record_id)
        except Exception:
            logger.warning("Invalid meeting source_record_id: %s", event.source_record_id)
            return preview

        assoc_table: Table = _table(CalendarEventDealAssociation)
        event_table: Table = _table(CalendarEvent)
        attendees_attr = cast(QueryableAttribute[Any], CalendarEvent.attendees)
        stmt = (
            select(CalendarEventDealAssociation, CalendarEvent)
            .outerjoin(
                CalendarEvent,
                (assoc_table.c.event_id == event_table.c.event_id)
                & (event_table.c.organisation_id == self.organisation_id),
            )
            .options(selectinload(attendees_attr))
            .where(
                assoc_table.c.organisation_id == self.organisation_id,
                assoc_table.c.association_id == association_id,
            )
        )
        result = await self.session.execute(stmt)
        row = result.first()
        if not row:
            return preview

        association, calendar_event = row
        if not association:
            return preview

        # Build attendee list
        attendees: list[str] = []
        duration_minutes: int | None = preview.get("duration_minutes")
        title: str | None = preview.get("title")

        if calendar_event:
            title = calendar_event.title or title

            if calendar_event.start_at and calendar_event.end_at:
                duration_minutes = int(
                    (calendar_event.end_at - calendar_event.start_at).total_seconds() / 60
                )

            # Collect attendees
            if calendar_event.organiser_email:
                attendees.append(calendar_event.organiser_email)

            att_list = getattr(calendar_event, "attendees", None)
            if att_list:
                for att in att_list:
                    email = getattr(att, "email", None)
                    if email and email not in attendees:
                        attendees.append(email)

        # Build meeting_summary block
        meeting_summary = None
        summary_status = _enum_value(association.meeting_summary_status)
        if summary_status or association.meeting_summary_data:
            meeting_summary = {
                "status": summary_status,
                "data": association.meeting_summary_data,
            }

        return {
            # Preview fields
            "title": title,
            "duration_minutes": duration_minutes,
            "attendee_count": len(attendees) if attendees else event.participant_count,
            # Detail enrichment
            "attendees": attendees if attendees else None,
            "meeting_summary": meeting_summary,
        }

    # -------------------------------------------------------------------------
    # Create/Update Methods (called from tasks)
    # -------------------------------------------------------------------------

    async def create_email_timeline_event(
        self,
        deal_id: UUID,
        association: DealEmailAssociation,
        email: Email | None = None,
        email_subject: str | None = None,
        email_date: datetime | None = None,
        from_name: str | None = None,
        from_email: str | None = None,
        participant_names: list[str] | None = None,
        participant_count: int = 0,
        event_source: TimelineEventSource | None = None,
    ) -> DealTimelineEvent:
        """
        Create a timeline event for an email association.

        Args:
            deal_id: Deal UUID
            association: The DealEmailAssociation record
            email: Optional Email record for additional context
            email_subject: Email subject (fallback if email not provided)
            email_date: Email date (fallback if email not provided)
            from_name: Sender name
            from_email: Sender email
            participant_names: List of participant names for preview
            participant_count: Total participant count
            event_source: Source system (EMAIL_GMAIL, EMAIL_OUTLOOK)

        Returns:
            Created DealTimelineEvent
        """
        resolved_source = event_source
        if resolved_source is None and email is not None:
            it_stmt = select(KnowledgeIntegration).where(
                _table(KnowledgeIntegration).c.integration_id == email.integration_id
            )
            it_result = await self.session.execute(it_stmt)
            integration = it_result.scalar_one_or_none()
            integration_type = getattr(integration, "integration_type", None)
            resolved_source = resolve_email_event_source(integration_type)
        resolved_source = resolved_source or TimelineEventSource.EMAIL_GMAIL

        # Extract data from email if provided
        subject = email_subject
        date = email_date
        sender_name = from_name
        sender_email = from_email

        if email:
            subject = subject or email.subject
            date = date or email.received_at
            sender_name = sender_name or email.from_name
            sender_email = sender_email or email.from_email

        # Use association data as fallback
        subject = subject or association.email_subject or "(No Subject)"
        date = date or association.email_date or association.created_at

        # Build subtitle
        subtitle = None
        if sender_name:
            subtitle = f"From: {sender_name}"
        elif sender_email:
            subtitle = f"From: {sender_email}"

        # Build metadata
        metadata: dict[str, Any] = {
            "internet_message_id": association.internet_message_id,
        }
        if email:
            metadata["has_attachments"] = email.has_attachments
            metadata["thread_id"] = email.thread_id

        timeline_event = DealTimelineEvent(
            organisation_id=self.organisation_id,
            deal_id=deal_id,
            event_type=TimelineEventType.EMAIL,
            event_source=resolved_source,
            event_timestamp=date,
            source_record_id=str(association.association_id),
            title=subject,
            subtitle=subtitle,
            direction=_enum_value(association.direction),
            status=_enum_value(association.status),
            participant_names=participant_names or [],
            participant_count=participant_count,
            event_metadata=metadata,
        )

        self.session.add(timeline_event)
        await self.session.flush()

        logger.debug(
            f"Created EMAIL timeline event for deal {deal_id}, "
            f"association {association.association_id}"
        )
        return timeline_event

    async def create_meeting_timeline_event(
        self,
        deal_id: UUID,
        association: CalendarEventDealAssociation,
        event: CalendarEvent | None = None,
        meeting_title: str | None = None,
        meeting_start: datetime | None = None,
        duration_minutes: int | None = None,
        participant_names: list[str] | None = None,
        participant_count: int = 0,
        event_source: TimelineEventSource | None = None,
    ) -> DealTimelineEvent:
        """
        Create a timeline event for a meeting association.

        Args:
            deal_id: Deal UUID
            association: The CalendarEventDealAssociation record
            event: Optional CalendarEvent for additional context
            meeting_title: Meeting title (fallback if event not provided)
            meeting_start: Meeting start time
            duration_minutes: Duration in minutes
            participant_names: List of participant names for preview
            participant_count: Total participant count
            event_source: Source system (CALENDAR_GOOGLE, CALENDAR_OUTLOOK, TEAMS)

        Returns:
            Created DealTimelineEvent
        """
        resolved_source = event_source
        if resolved_source is None and event is not None:
            calendar_integration_type = None
            if getattr(event, "integration_id", None):
                it_stmt = select(KnowledgeIntegration).where(
                    _table(KnowledgeIntegration).c.integration_id == event.integration_id
                )
                it_result = await self.session.execute(it_stmt)
                integration = it_result.scalar_one_or_none()
                calendar_integration_type = getattr(integration, "integration_type", None)
            else:
                # Fallback for shared calendar events: pick any user-state integration.
                state_table: Table = _table(CalendarEventUserState)
                state_stmt = (
                    select(state_table.c.integration_id)
                    .where(state_table.c.event_id == event.event_id)
                    .limit(1)
                )
                state_result = await self.session.execute(state_stmt)
                integration_id = state_result.scalar_one_or_none()
                if integration_id is not None:
                    it_stmt = select(KnowledgeIntegration).where(
                        _table(KnowledgeIntegration).c.integration_id == integration_id
                    )
                    it_result = await self.session.execute(it_stmt)
                    integration = it_result.scalar_one_or_none()
                    calendar_integration_type = getattr(integration, "integration_type", None)

            resolved_source = resolve_meeting_event_source(event, calendar_integration_type)
        resolved_source = resolved_source or TimelineEventSource.CALENDAR_GOOGLE

        title = meeting_title
        start_at = meeting_start
        duration = duration_minutes

        if event:
            title = title or event.title
            start_at = start_at or event.start_at
            if event.start_at and event.end_at:
                duration = int((event.end_at - event.start_at).total_seconds() / 60)
            participant_count = participant_count or event.attendee_count

        title = title or "Meeting"
        start_at = start_at or association.created_at

        # Build subtitle
        subtitle = None
        if duration:
            subtitle = f"Duration: {duration} mins"

        # Build metadata
        metadata: dict[str, Any] = {}
        if duration:
            metadata["duration_minutes"] = duration
        if event:
            metadata["location"] = event.location
            metadata["conference_link"] = event.conference_link
        if association.meeting_prep_status:
            metadata["meeting_prep_status"] = _enum_value(association.meeting_prep_status)
        if association.meeting_summary_status:
            metadata["meeting_summary_status"] = _enum_value(association.meeting_summary_status)

        timeline_event = DealTimelineEvent(
            organisation_id=self.organisation_id,
            deal_id=deal_id,
            event_type=TimelineEventType.MEETING,
            event_source=resolved_source,
            event_timestamp=start_at,
            source_record_id=str(association.association_id),
            title=title,
            subtitle=subtitle,
            direction=None,  # Meetings don't have direction
            status=_enum_value(association.status),
            participant_names=participant_names or [],
            participant_count=participant_count,
            event_metadata=metadata,
        )

        self.session.add(timeline_event)
        await self.session.flush()

        logger.debug(
            f"Created MEETING timeline event for deal {deal_id}, "
            f"association {association.association_id}"
        )
        return timeline_event

    async def exclude_timeline_event(
        self, timeline_event_id: UUID, excluded_at: datetime | None = None
    ) -> bool:
        """
        Mark a timeline event as excluded (soft-hide from timeline).

        Args:
            timeline_event_id: UUID of the timeline event
            excluded_at: Timestamp to set (defaults to now)

        Returns:
            True if updated, False if not found
        """
        from datetime import UTC

        excluded_at = excluded_at or datetime.now(UTC)

        t: Table = _table(DealTimelineEvent)
        stmt = (
            update(t)
            .where(
                t.c.organisation_id == self.organisation_id,
                t.c.timeline_event_id == timeline_event_id,
            )
            .values(excluded_at=excluded_at, updated_at=datetime.now(UTC))
        )

        result = await self.session.execute(stmt)
        return result.rowcount > 0

    async def update_timeline_event_status(self, timeline_event_id: UUID, status: str) -> bool:
        """
        Update the status of a timeline event.

        Args:
            timeline_event_id: UUID of the timeline event
            status: New status value

        Returns:
            True if updated, False if not found
        """
        from datetime import UTC

        t: Table = _table(DealTimelineEvent)
        stmt = (
            update(t)
            .where(
                t.c.organisation_id == self.organisation_id,
                t.c.timeline_event_id == timeline_event_id,
            )
            .values(status=status, updated_at=datetime.now(UTC))
        )

        result = await self.session.execute(stmt)
        return result.rowcount > 0

    async def get_existing_timeline_event(
        self,
        deal_id: UUID,
        event_type: TimelineEventType,
        source_record_id: str,
    ) -> DealTimelineEvent | None:
        """
        Check if a timeline event already exists for a source record.

        Args:
            deal_id: Deal UUID
            event_type: Type of event
            source_record_id: ID of the source record

        Returns:
            Existing timeline event or None
        """
        t: Table = _table(DealTimelineEvent)
        stmt = select(DealTimelineEvent).where(
            t.c.organisation_id == self.organisation_id,
            t.c.deal_id == deal_id,
            t.c.event_type == event_type.value,
            t.c.source_record_id == source_record_id,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()
