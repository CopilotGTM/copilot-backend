"""Database writer for calendar sync rows.

This module owns every SQL statement the calendar pipeline issues. The
orchestrator (sync_service) decides *which* events to write and *whether* to
write them; this writer decides *how* — Postgres `INSERT ... ON CONFLICT DO
UPDATE` with bulk values, deletes-then-inserts for attendee snapshots, and the
exact column lists that survive an upsert.

Two contracts the writer protects:

  1. **Batched bulk writes.** The whole reason this module was extracted
     from the legacy service was an N+1 in production: every changed event was
     producing one INSERT for calendar_events, one delete + one insert for
     attendees, and one INSERT for user_state. The batched methods here collapse
     each of those to one statement per sync pass when safe, or a small number
     of parameter-safe statements for very large event batches; per-event
     methods exist for the full-sync path that still operates one event at a
     time.

  2. **Attendee snapshots are atomic replacements, not diffs.** When the provider
     sends a roster, we delete the old roster for that event and re-insert the
     new one in the same session. This matches the provider contract (each
     payload is the truth as of that moment) and avoids having to diff
     individual attendee rows.

The session is supplied by the caller and not closed here — sync_service owns
session lifetime so it can release the org connection at the right boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.calendar import (
    CalendarAttendee,
    CalendarEvent,
    CalendarEventUserState,
)
from src.infra.utils.logger import get_logger
from src.service.integrations.calendar.types import (
    CalendarEventUpsertBatchItem,
    NormalizedCalendarEvent,
)

logger = get_logger(__name__)

# Cap the number of debug samples we attach to a Sentry log line. Calendar
# batches can include hundreds of events; logging all of them on a write
# failure would be noisy in Sentry and slow to render.
SAMPLE_LOG_LIMIT = 5
_ASYNC_PG_BIND_PARAMETER_LIMIT = 32_767
_BULK_UPSERT_PARAMETER_BUDGET = 24_000
SPARSE_OPTIONAL_EVENT_COLUMNS = frozenset(
    {
        "calendar_external_id",
        "conference_link",
        "online_meeting_id",
        "online_meeting_provider",
        "organiser_name",
        "timezone",
    }
)


class CalendarSyncWriter:
    """Owns calendar_events, attendee snapshots, and user-state persistence.

    Caller (sync_service) supplies an AsyncSession scoped to the right org
    schema and the organisation_id boundary. The writer trusts both inputs and
    does not re-validate org ownership — sync_service is responsible for
    acquiring sessions through `acquire_org_async_session` so the search_path is
    already pinned to the correct tenant schema before any statement here runs.
    """

    def __init__(self, session: AsyncSession, organisation_id: UUID) -> None:
        self.session = session
        self.organisation_id = organisation_id

    async def upsert_event(
        self,
        _integration: KnowledgeIntegration,
        event: NormalizedCalendarEvent,
    ) -> CalendarEvent:
        """Single-event upsert used by the full-sync path.

        Runs one INSERT ... ON CONFLICT DO UPDATE keyed on
        (external_id, occurrence_anchor) and returns the row that was written.
        Incremental sync uses `batch_upsert_events` instead; this method is
        deliberately kept for the slower per-event flow because full sync still
        wants per-event error isolation.
        """
        if not event.identity.external_id:
            raise ValueError("Cannot upsert calendar event without external_id")

        values = self._build_event_upsert_values(event)
        stmt: Any = insert(CalendarEvent).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["external_id", "occurrence_anchor"],
            set_=self._event_upsert_update_dict(stmt, values),
        ).returning(CalendarEvent)

        result = await self.session.execute(stmt)
        return cast(CalendarEvent, result.scalars().one())

    def _build_event_upsert_values(self, event: NormalizedCalendarEvent) -> dict[str, Any]:
        if not event.identity.external_id:
            raise ValueError("Cannot build calendar event values without external_id")

        # `synced_at`, `created_at`, and `updated_at` are all set to the same
        # `now` so a fresh insert and a same-cycle update produce comparable
        # timestamps. ON CONFLICT will skip `created_at` (see the update dict)
        # so existing rows keep their original creation time.
        now = datetime.now(UTC)
        return {
            "organisation_id": self.organisation_id,
            "external_id": event.identity.external_id,
            "provider": event.provider,
            "occurrence_anchor": event.identity.occurrence_anchor,
            "original_start_at": event.identity.original_start_at,
            "data_payload": event.data_payload,
            "synced_at": now,
            "created_at": now,
            "updated_at": now,
            **event.fields,
        }

    @staticmethod
    def _event_upsert_update_dict(stmt: Any, values: dict[str, Any]) -> dict[str, Any]:
        # On conflict we update every column *except* the identity columns and
        # created_at. event_id is the PK, the (external_id, occurrence_anchor)
        # pair is the conflict target, organisation_id never changes per row,
        # and we want created_at preserved so audit reports stay accurate.
        update_values: dict[str, Any] = {}
        for key in values:
            if key in (
                "event_id",
                "organisation_id",
                "external_id",
                "occurrence_anchor",
                "created_at",
            ):
                continue

            excluded_value = getattr(stmt.excluded, key)
            if key in SPARSE_OPTIONAL_EVENT_COLUMNS:
                update_values[key] = func.coalesce(excluded_value, getattr(CalendarEvent, key))
            else:
                update_values[key] = excluded_value

        return update_values

    @staticmethod
    def _normalize_event_batch_values(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Give SQLAlchemy a consistent column set for every bulk INSERT row."""
        if not values:
            return []

        all_keys = sorted(set().union(*(row.keys() for row in values)))
        return [{key: row.get(key) for key in all_keys} for row in values]

    @staticmethod
    def _iter_parameter_safe_chunks(
        values: list[dict[str, Any]],
    ) -> list[list[dict[str, Any]]]:
        if not values:
            return []

        max_rows_per_statement = max(1, _BULK_UPSERT_PARAMETER_BUDGET // len(values[0]))
        return [
            values[offset : offset + max_rows_per_statement]
            for offset in range(0, len(values), max_rows_per_statement)
        ]

    async def _execute_event_upsert_values(
        self,
        values: list[dict[str, Any]],
    ) -> list[CalendarEvent]:
        stmt: Any = insert(CalendarEvent).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["external_id", "occurrence_anchor"],
            set_=self._event_upsert_update_dict(stmt, values[0]),
        ).returning(CalendarEvent)

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def batch_upsert_events(
        self,
        integration: KnowledgeIntegration,
        items: list[CalendarEventUpsertBatchItem],
    ) -> dict[tuple[str, str], CalendarEvent]:
        """Upsert changed calendar rows in one statement for incremental sync.

        Returns a map keyed by (external_id, occurrence_anchor) so the caller
        can correlate each input item to the row that came back from RETURNING.
        On failure, logs a bounded sample of the input payloads to Sentry and
        re-raises — sync_service uses the failure to fail the whole sync pass
        rather than silently dropping events.
        """
        if not items:
            return {}

        # The provider can deliver the same shared-UID event more than once in
        # one batch (multiple attendees on the same meeting; our integration
        # owns the mailbox for each). Pre-collapse so the bulk INSERT does not
        # try to insert duplicate primary-conflict rows in a single statement,
        # which Postgres rejects. The last item wins, matching the old
        # per-event upsert behavior.
        latest_by_identity: dict[tuple[str, str], CalendarEventUpsertBatchItem] = {}
        for item in items:
            if not item.event.identity.external_id:
                continue
            latest_by_identity[
                (item.event.identity.external_id, item.event.identity.occurrence_anchor)
            ] = item

        if not latest_by_identity:
            return {}

        raw_values = [
            self._build_event_upsert_values(item.event) for item in latest_by_identity.values()
        ]
        values = self._normalize_event_batch_values(raw_values)
        events: list[CalendarEvent] = []
        chunks = self._iter_parameter_safe_chunks(values)
        for chunk_index, chunk in enumerate(chunks, start=1):
            try:
                events.extend(await self._execute_event_upsert_values(chunk))
            except Exception:
                # Attach a bounded sample of the input payloads so the Sentry event
                # has enough context to debug the failure without dumping the whole
                # batch (which can be hundreds of events).
                logger.error(
                    "Calendar batch event upsert failed: integration=%s chunk=%s/%s "
                    "chunk_size=%s samples=%s",
                    integration.integration_id,
                    chunk_index,
                    len(chunks),
                    len(chunk),
                    [
                        item.event.debug_sample
                        for item in list(latest_by_identity.values())[:SAMPLE_LOG_LIMIT]
                    ],
                    exc_info=True,
                )
                raise

        return {(event.external_id, event.occurrence_anchor): event for event in events}

    @staticmethod
    def build_user_state_row(
        event_id: UUID,
        integration_id: UUID,
        external_id: str,
        series_external_id: str | None,
    ) -> tuple[UUID, UUID, str, str | None]:
        """Build the tuple shape sync_service batches for user-state upserts."""
        return (event_id, integration_id, external_id, series_external_id)

    @staticmethod
    def build_user_state_values(
        rows: list[tuple[UUID, UUID, str, str | None]],
    ) -> list[dict[str, Any]]:
        """Collapse user-state tuples into the column-keyed dicts the bulk insert expects.

        Pre-collapses by (event_id, integration_id) — same reason as
        `batch_upsert_events` — so the bulk INSERT never tries to insert two
        rows that conflict on the same unique key in one statement.
        """
        now = datetime.now(UTC)
        latest_by_key: dict[tuple[UUID, UUID], tuple[UUID, UUID, str, str | None]] = {}
        for event_id, integration_id, external_id, series_external_id in rows:
            latest_by_key[(event_id, integration_id)] = (
                event_id,
                integration_id,
                external_id,
                series_external_id,
            )

        return [
            {
                "event_id": event_id,
                "integration_id": integration_id,
                "external_id": external_id,
                "series_external_id": series_external_id,
                "is_deleted": False,
                "created_at": now,
                "updated_at": now,
            }
            for event_id, integration_id, external_id, series_external_id in latest_by_key.values()
        ]

    @staticmethod
    def _user_state_update_dict(stmt: Any) -> dict[str, Any]:
        # On conflict we refresh the per-user external_id (provider IDs can
        # change for the same conceptual event), reset is_deleted to False
        # because a re-sync of an active event "un-deletes" it from the
        # integration's perspective, and bump updated_at.
        return {
            "external_id": stmt.excluded.external_id,
            "series_external_id": stmt.excluded.series_external_id,
            "is_deleted": False,
            "updated_at": stmt.excluded.updated_at,
        }

    async def batch_upsert_user_states(
        self,
        rows: list[tuple[UUID, UUID, str, str | None]],
    ) -> None:
        """Upsert per-integration event state in one statement.

        User-state rows track which integrations can currently see which event
        (one row per (event, integration) pair). They are written for *every*
        synced event, even unchanged ones, so a deletion delivered against this
        integration's external_id can later be attributed correctly.
        """
        if not rows:
            return

        values = self.build_user_state_values(rows)
        stmt = insert(CalendarEventUserState).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["event_id", "integration_id"],
            set_=self._user_state_update_dict(stmt),
        )
        await self.session.execute(stmt)

    async def upsert_user_state(
        self,
        event: CalendarEvent,
        integration: KnowledgeIntegration,
        external_id: str,
        series_external_id: str | None = None,
    ) -> CalendarEventUserState:
        """Single-row user-state upsert used by full calendar sync."""
        values = self.build_user_state_values(
            [
                self.build_user_state_row(
                    event.event_id,
                    integration.integration_id,
                    external_id,
                    series_external_id,
                )
            ]
        )[0]

        stmt: Any = insert(CalendarEventUserState).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["event_id", "integration_id"],
            set_=self._user_state_update_dict(stmt),
        ).returning(CalendarEventUserState)

        result = await self.session.execute(stmt)
        return cast(CalendarEventUserState, result.scalars().one())

    async def soft_delete_user_state(
        self,
        event: CalendarEvent,
        *,
        integration_id: UUID,
        external_id: str,
    ) -> bool:
        """Delete one mailbox copy and cancel the shared row only when none remain.

        Calendar sync and organizer-triggered cancellation share this invariant:
        deleting one attendee's copy must not cancel an event still visible through
        another active integration in the organisation.
        """

        stmt = select(CalendarEventUserState).where(
            col(CalendarEventUserState.event_id) == event.event_id,
            col(CalendarEventUserState.integration_id) == integration_id,
            col(CalendarEventUserState.external_id) == external_id,
        )
        result = await self.session.execute(stmt)
        state = result.scalar_one_or_none()
        if state is None:
            return False
        state.is_deleted = True
        state.updated_at = datetime.now(UTC)
        self.session.add(state)

        remaining = await self.session.execute(
            select(CalendarEventUserState).where(
                col(CalendarEventUserState.event_id) == event.event_id,
                col(CalendarEventUserState.is_deleted).is_(False),
                col(CalendarEventUserState.state_id) != state.state_id,
            )
        )
        if remaining.scalars().first() is None:
            event.status = "cancelled"
            event.synced_at = datetime.now(UTC)
            self.session.add(event)
        return True

    async def batch_sync_attendees(
        self,
        event_snapshots: list[tuple[CalendarEvent, NormalizedCalendarEvent]],
    ) -> None:
        """Refresh attendee snapshots for changed events in bulk.

        Snapshots are atomic replacements: one bulk DELETE for every affected
        event, then one bulk INSERT for the new roster. Doing this per-event
        was the second N+1 the writer split solved (the first being event rows).
        """
        snapshots_by_event: dict[UUID, tuple[CalendarEvent, NormalizedCalendarEvent]] = {}
        for db_event, normalized in event_snapshots:
            # Skip events whose payload didn't include any attendee data —
            # absence of an attendee key means "no roster info this cycle",
            # not "the roster is now empty", so we leave the existing rows
            # alone. See `has_attendee_snapshot` in normalizer.
            if normalized.has_attendee_snapshot:
                snapshots_by_event[db_event.event_id] = (db_event, normalized)

        if not snapshots_by_event:
            return

        event_ids = list(snapshots_by_event)
        # Snapshot semantics: drop the existing roster for these events, then
        # insert the new one. The unique index on (event_id, email) would
        # otherwise force an upsert that either skips new attendees or has to
        # reconcile row by row. Two bulk statements is one round trip per
        # operation and preserves the atomic-replacement contract.
        await self.session.execute(
            delete(CalendarAttendee).where(col(CalendarAttendee.event_id).in_(event_ids))
        )

        values: list[dict[str, Any]] = []
        for db_event, normalized in snapshots_by_event.values():
            values.extend(self._build_attendee_values(db_event, normalized))

        for chunk in self._iter_parameter_safe_chunks(values):
            await self.session.execute(insert(CalendarAttendee).values(chunk))

    async def sync_attendees(
        self, event: CalendarEvent, normalized: NormalizedCalendarEvent
    ) -> None:
        """Refresh attendees for one full-sync event.

        Same delete-then-insert contract as the batched version, scoped to a
        single event. Used by the full-sync path which still operates one event
        at a time for stronger per-event error isolation.
        """
        if not normalized.has_attendee_snapshot:
            return

        # Same atomic-replacement contract as `batch_sync_attendees`: delete
        # the existing roster for this event, then insert the new one.
        await self.session.execute(
            delete(CalendarAttendee).where(col(CalendarAttendee.event_id) == event.event_id)
        )

        values = self._build_attendee_values(event, normalized)
        if values:
            await self.session.execute(insert(CalendarAttendee).values(values))

    @staticmethod
    def _build_attendee_values(
        event: CalendarEvent,
        normalized: NormalizedCalendarEvent,
    ) -> list[dict[str, Any]]:
        return [
            {
                "attendee_id": uuid4(),
                "event_id": event.event_id,
                "email": attendee.email,
                "display_name": attendee.display_name,
                "response_status": attendee.response_status,
                "is_organiser": attendee.is_organiser,
                "data_payload": attendee.data_payload,
            }
            for attendee in normalized.attendees
        ]
