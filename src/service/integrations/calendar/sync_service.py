"""Calendar sync orchestration — full and incremental sync into canonical tables.

Called by the integrations worker. Two entry points:

  - `sync_calendar_events`            — full sync. Walks every event the
                                        provider returned, upserts one at a
                                        time, refreshes attendees per event.
                                        Used after a fresh OAuth grant or when
                                        the cursor has been invalidated.

  - `sync_calendar_events_incremental` — delta sync. Same per-event routing for
                                        cancellations/deletions, but DB writes
                                        are batched into single statements per
                                        sync pass. This is the default scheduled
                                        path and the one that solved the prod
                                        N+1 we used to see in Sentry.

Both methods own sync-history bookkeeping (start/complete) and integration
status updates. Provider parsing lives in `normalizer`, change detection in
`change_detector`, and SQL writes in `writer` — this file is purely orchestration.

The session is supplied by the caller. Sync_service does not call providers
directly (the calendar adapters do that and pass the parsed payloads in), so
session-release timing is the caller's concern, not ours. We do issue many DB
statements per pass, so callers should expect this service to hold the session
for the duration of one sync.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.organisation_tables.sync import KnowledgeIntegration
from src.domain.models.organisation_tables.sync.calendar import (
    CalendarEvent,
    CalendarEventUserState,
)
from src.domain.models.organisation_tables.sync.integration_models import SyncStatus
from src.infra.utils.logger import get_logger
from src.service.integrations.calendar.change_detector import has_meaningful_event_changes
from src.service.integrations.calendar.normalizer import CalendarEventNormalizer
from src.service.integrations.calendar.types import (
    CalendarEventUpsertBatchItem,
    NormalizedCalendarEvent,
)
from src.service.integrations.calendar.writer import CalendarSyncWriter
from src.service.integrations.sync_history_manager import (
    SyncHistoryManager,
    datetime_now_naive,
)
from src.service.meeting_bot.recording_window import (
    is_within_recording_dispatch_window,
    normalize_utc,
)
from src.workers.deal_worker.client import send_deal_task_async
from src.workers.integrations_worker.utils.sync_metadata import (
    SyncAction,
    SyncActionCounts,
    extract_sync_action,
)

logger = get_logger(__name__)

# Cap the number of debug samples we attach to a Sentry log line. Calendar
# batches can include hundreds of events; logging all of them would be noisy
# in Sentry and slow to render.
SAMPLE_LOG_LIMIT = 5


class CalendarSyncService:
    """Sync provider calendar events into canonical calendar tables.

    The adapters call this service with raw Google or Microsoft payloads. This
    class owns sync history and counters; provider parsing and SQL writes stay
    in separate modules so incremental sync can batch DB work without changing
    the per-event routing flow.

    Lifetime: instantiated once per sync pass. Holds an AsyncSession scoped to
    the right org schema (set up by the worker before calling in) and the
    organisation_id boundary the writer trusts.
    """

    def __init__(self, session: AsyncSession, organisation_id: UUID) -> None:
        self.db = session
        self.organisation_id = organisation_id
        self._sync_history = SyncHistoryManager(session)
        self._normalizer = CalendarEventNormalizer()
        self._writer = CalendarSyncWriter(session, organisation_id)
        self._pending_recording_event_ids: set[UUID] = set()

    async def sync_calendar_events(
        self,
        *,
        integration: KnowledgeIntegration,
        events: Iterable[dict[str, Any]],
        provider: str,
    ) -> dict[str, Any]:
        """Full calendar event sync — one event at a time.

        Used after a fresh OAuth grant or when the incremental cursor has been
        invalidated. Walks every event the adapter returned, upserts each one
        individually with its attendee snapshot, and commits in batches of 100
        so a long sync does not hold one huge transaction.

        Errors per event are isolated (logged, counted as `records_failed`,
        sync continues). Errors at the sync_history or integration-status level
        are fatal — they roll the integration back to FAILED and re-raise.
        """
        sync_history = await self._sync_history.start(integration.integration_id)

        try:
            event_list = list(events)
            logger.info(
                "sync_calendar_events started: integration=%s provider=%s event_count=%d",
                integration.integration_id,
                provider,
                len(event_list),
            )

            records_processed = 0
            records_failed = 0
            created_count = 0
            updated_count = 0
            changed_event_ids: list[str] = []
            missing_identity_samples: list[dict[str, Any]] = []

            # --- Step 1: prefetch existing rows for every (external_id, anchor) ---
            # We look up all identities in one query so the per-event loop below
            # can decide is_new without making a round trip per event.
            existing = await self._load_existing_events_by_identity(
                [
                    (external_id, occurrence_anchor)
                    for external_id, occurrence_anchor, _ in (
                        self._extract_identity(event_data, provider) for event_data in event_list
                    )
                    if external_id
                ]
            )

            # --- Step 2: walk every event, routing per-event ---
            for idx, event_data in enumerate(event_list):
                try:
                    normalized = self._normalizer.normalize(event_data, provider)
                    external_id = normalized.identity.external_id
                    occurrence_anchor = normalized.identity.occurrence_anchor
                    if not external_id:
                        # Without a shared global UID we can't link this event
                        # to any other attendee's copy. Log a sample and skip;
                        # the same UID may appear on a future sync once the
                        # provider stabilises the payload.
                        logger.warning("Event %d: skipping (missing shared global UID)", idx)
                        if len(missing_identity_samples) < SAMPLE_LOG_LIMIT:
                            missing_identity_samples.append(normalized.debug_sample)
                        records_failed += 1
                        continue

                    key = (external_id, occurrence_anchor)
                    is_new = key not in existing
                    event = await self._upsert_event(integration, normalized)
                    await self._sync_attendees(event, normalized)
                    user_external_id = normalized.user_external_id or external_id
                    await self._upsert_user_state(
                        event,
                        integration,
                        user_external_id,
                        normalized.series_external_id,
                    )
                    existing[key] = event
                    changed_event_ids.append(str(event.event_id))
                    self._queue_recording_dispatch_if_eligible(event)

                    if is_new:
                        created_count += 1
                    else:
                        updated_count += 1

                    records_processed += 1

                    # Commit every 100 events. Full syncs can carry thousands
                    # of events for active mailboxes; one giant transaction
                    # would hold locks too long and amplify rollback cost on
                    # any single failure.
                    if records_processed % 100 == 0:
                        await self.db.commit()
                        await self._dispatch_pending_recording_events()
                        logger.debug("Batch commit: %d events processed", records_processed)

                except Exception as e:
                    # Per-event failures are isolated so a single bad payload
                    # cannot abort the rest of the sync. Counted via
                    # records_failed; not re-raised.
                    logger.error(
                        "Failed to sync event %d (provider_id=%s): %s",
                        idx,
                        event_data.get("id"),
                        str(e),
                        exc_info=True,
                    )
                    records_failed += 1

            await self.db.commit()
            await self._dispatch_pending_recording_events()

            if missing_identity_samples:
                logger.info(
                    "Calendar sync sample missing-identity events: integration=%s samples=%s",
                    integration.integration_id,
                    missing_identity_samples,
                )

            # --- Step 3: close out sync history and integration status ---
            await self._sync_history.complete(
                sync_history,
                status=SyncStatus.SUCCESS,
                records_processed=records_processed,
                records_failed=records_failed,
                metadata={
                    "created": created_count,
                    "updated": updated_count,
                    "provider": provider,
                },
            )

            integration.last_synced_at = datetime_now_naive()
            integration.last_sync_status = SyncStatus.SUCCESS.value
            self.db.add(integration)
            await self.db.commit()

            return {
                "status": "success",
                "records_processed": records_processed,
                "records_failed": records_failed,
                "created": created_count,
                "updated": updated_count,
                "changed_event_ids": changed_event_ids,
            }

        except Exception as e:
            logger.error(
                "Calendar sync failed: integration=%s error=%s",
                integration.integration_id,
                str(e),
                exc_info=True,
            )

            # Mark the sync history failed and roll the integration to FAILED so
            # the worker UI surfaces the problem on next render. Truncate the
            # error to 500 chars — the column is bounded and stack traces are
            # already in Sentry via exc_info above.
            await self._sync_history.complete(
                sync_history,
                status=SyncStatus.FAILED,
                records_processed=0,
                records_failed=len(event_list),
                error_details={"error": str(e), "error_type": type(e).__name__},
            )

            integration.last_sync_status = SyncStatus.FAILED.value
            integration.last_sync_error = str(e)[:500]
            self.db.add(integration)
            await self.db.commit()

            raise

    async def sync_calendar_events_incremental(
        self,
        *,
        integration: KnowledgeIntegration,
        events: Iterable[dict[str, Any]],
        provider: str,
    ) -> dict[str, Any]:
        """Sync calendar events using batched DB writes for the incremental path.

        This is the scheduled-tick path. Per-event routing for cancellations and
        deletions is preserved (same branches as the legacy implementation), but
        the actual `calendar_events`, `calendar_attendees`, and
        `calendar_event_user_state` writes are accumulated and flushed as bulk
        statements at the end. That collapse is what removed the prod N+1 we
        used to see in Sentry on every scheduled run.

        Counter contract (the trailing log line and metadata both depend on it):
        every input event is accounted for in exactly one bucket — created,
        updated, skipped, cancelled_existing, cancelled_no_record,
        deleted_processed, deleted_no_state, or records_failed. The
        `unaccounted` value in the log line should always be 0; non-zero means
        the routing logic missed a branch and needs investigation.
        """
        sync_history = await self._sync_history.start(integration.integration_id)

        try:
            event_list = list(events)
            logger.info(
                "sync_calendar_events_incremental started: integration=%s provider=%s event_count=%d",
                integration.integration_id,
                provider,
                len(event_list),
            )

            # Empty-batch fast path: providers can deliver an empty delta when
            # nothing changed since the last cursor. Close out sync history
            # cleanly and return without touching the writer.
            if not event_list:
                await self._sync_history.complete(
                    sync_history,
                    status=SyncStatus.SUCCESS,
                    records_processed=0,
                    records_failed=0,
                    metadata={"provider": provider, "skipped": 0},
                )
                return {
                    "status": "success",
                    "records_processed": 0,
                    "records_failed": 0,
                    "created": 0,
                    "updated": 0,
                    "skipped": 0,
                    "changed_event_ids": [],
                }

            action_counts = SyncActionCounts()

            # --- Step 1: prefetch existing rows for every (external_id, anchor) ---
            # One query covers the whole batch. The per-event loop reads from
            # `existing` instead of doing N round trips, which is half of the
            # N+1 fix (the other half is the batched writes at the end).
            existing = await self._load_existing_events_by_identity(
                [
                    (external_id, occurrence_anchor)
                    for external_id, occurrence_anchor, _ in (
                        self._extract_identity(event_data, provider) for event_data in event_list
                    )
                    if external_id
                ]
            )

            records_processed = 0
            records_failed = 0
            created_count = 0
            updated_count = 0
            skipped_count = 0
            cancelled_existing_count = 0
            cancelled_no_record_count = 0
            deleted_processed_count = 0
            deleted_no_state_count = 0
            missing_identity_samples: list[dict[str, Any]] = []
            unchanged_samples: list[dict[str, Any]] = []
            directly_changed_event_ids: list[str] = []
            # These three lists accumulate the work that will be flushed as
            # batched writes after the per-event loop completes. Keeping them
            # local (rather than calling the writer per event) is the second
            # half of the N+1 fix.
            events_to_upsert: list[CalendarEventUpsertBatchItem] = []
            user_states_to_upsert: list[tuple[UUID, UUID, str, str | None]] = []
            attendee_snapshots: list[tuple[CalendarEvent, NormalizedCalendarEvent]] = []

            # --- Step 2: per-event routing — classify, accumulate writes ---
            for idx, event_data in enumerate(event_list):
                try:
                    normalized = self._normalizer.normalize(event_data, provider)
                    identity = normalized.identity
                    external_id = identity.external_id
                    occurrence_anchor = identity.occurrence_anchor
                    user_external_id = normalized.user_external_id
                    sync_action = extract_sync_action(event_data)
                    if sync_action:
                        action_counts.increment(sync_action)

                    # Branch A: explicit DELETED action from the adapter.
                    # Mark this integration's user-state row as deleted; only
                    # cancel the shared event if no other integration still
                    # has it active. This is what prevents one attendee's
                    # deletion from cancelling the meeting for everyone.
                    if sync_action == SyncAction.DELETED and user_external_id:
                        record = await self._find_event_by_user_external_id(
                            user_external_id,
                            integration.integration_id,
                        )
                        records = [(record, user_external_id)] if record else []
                        if not records:
                            records = await self._find_events_by_series_external_id(
                                user_external_id,
                                integration.integration_id,
                            )
                        if await self._mark_user_state_records_deleted(
                            records,
                            integration.integration_id,
                            directly_changed_event_ids,
                        ):
                            deleted_processed_count += 1
                            records_processed += 1
                        else:
                            deleted_no_state_count += 1
                        continue

                    # Branch B: Google sends standalone cancelled events
                    # without an iCalUID for some recurring deletions. They
                    # are *not* the well-formed exception case (which has
                    # both recurringEventId and originalStartTime) — those
                    # are handled by the normal upsert path below. Resolve
                    # via user_state so one attendee's cancel doesn't drop
                    # the event for other attendees.
                    if (
                        provider == "google"
                        and normalized.is_cancelled
                        and not self._is_google_cancelled_exception(event_data, provider)
                        and user_external_id
                    ):
                        record = await self._find_event_by_user_external_id(
                            user_external_id,
                            integration.integration_id,
                        )
                        records = [(record, user_external_id)] if record else []
                        if not records:
                            records = await self._find_events_by_series_external_id(
                                user_external_id,
                                integration.integration_id,
                            )
                        if await self._mark_user_state_records_deleted(
                            records,
                            integration.integration_id,
                            directly_changed_event_ids,
                        ):
                            cancelled_existing_count += 1
                            records_processed += 1
                        else:
                            cancelled_no_record_count += 1
                        continue

                    # Branch C: no shared UID. Last-chance handling for the
                    # Google cancelled-without-UID case where the user-state
                    # path didn't hit (e.g., we never saw the original event,
                    # so user_state lookup failed). Otherwise, log a sample
                    # and count as failed — see the warning in the full-sync
                    # path for context.
                    if not external_id:
                        if provider == "google" and normalized.is_cancelled and user_external_id:
                            record = await self._find_event_by_user_external_id(
                                user_external_id,
                                integration.integration_id,
                            )
                            records = [(record, user_external_id)] if record else []
                            if not records and normalized.series_external_id and occurrence_anchor:
                                record_with_state = await self._find_event_by_series_external_id_and_occurrence_anchor(
                                    normalized.series_external_id,
                                    occurrence_anchor,
                                    integration.integration_id,
                                )
                                records = [record_with_state] if record_with_state else []
                            if await self._mark_user_state_records_deleted(
                                records,
                                integration.integration_id,
                                directly_changed_event_ids,
                            ):
                                records_processed += 1
                                cancelled_existing_count += 1
                                continue
                        logger.warning("Event %d: skipping (missing shared global UID)", idx)
                        if len(missing_identity_samples) < SAMPLE_LOG_LIMIT:
                            missing_identity_samples.append(normalized.debug_sample)
                        records_failed += 1
                        continue

                    key = (external_id, occurrence_anchor)
                    record = existing.get(key)
                    is_new = record is None

                    # Branch D: well-formed cancellation with a shared UID.
                    # Flip the existing row's status to "cancelled" (if we
                    # have one), then queue a user-state refresh and an
                    # attendee snapshot replace alongside the normal batch.
                    if normalized.is_cancelled:
                        if record:
                            record.status = "cancelled"
                            record.synced_at = datetime.now(UTC)
                            self.db.add(record)
                            directly_changed_event_ids.append(str(record.event_id))
                            self._queue_recording_dispatch_if_eligible(record)
                            if user_external_id:
                                user_states_to_upsert.append(
                                    self._build_user_state_row(
                                        record.event_id,
                                        integration.integration_id,
                                        user_external_id,
                                        normalized.series_external_id,
                                    )
                                )
                            attendee_snapshots.append((record, normalized))
                            cancelled_existing_count += 1
                            records_processed += 1
                        else:
                            cancelled_no_record_count += 1
                        continue

                    # Branch E: normal create or update path.
                    if is_new:
                        events_to_upsert.append(CalendarEventUpsertBatchItem(event=normalized))
                        records_processed += 1
                        created_count += 1
                    else:
                        assert record is not None
                        if self._has_meaningful_event_changes(record, normalized):
                            events_to_upsert.append(CalendarEventUpsertBatchItem(event=normalized))
                            records_processed += 1
                            updated_count += 1
                        else:
                            # Unchanged event: no row write, no attendee
                            # rewrite, but we still refresh user_state so a
                            # later deletion against this integration's
                            # external_id can be attributed correctly.
                            if user_external_id:
                                user_states_to_upsert.append(
                                    self._build_user_state_row(
                                        record.event_id,
                                        integration.integration_id,
                                        user_external_id,
                                        normalized.series_external_id,
                                    )
                                )
                            if len(unchanged_samples) < SAMPLE_LOG_LIMIT:
                                unchanged_samples.append(normalized.debug_sample)
                            skipped_count += 1
                            action_counts.unchanged += 1
                            continue

                except Exception as e:
                    # Same per-event isolation contract as the full-sync path.
                    logger.error(
                        "Failed to sync event %d (provider_id=%s): %s",
                        idx,
                        event_data.get("id"),
                        str(e),
                        exc_info=True,
                    )
                    records_failed += 1

            # --- Step 3: flush the batched writes ---
            # Per-event routing above preserves legacy counters and error
            # isolation; only the DB writes are batched to remove the prod
            # N+1 query pattern.
            upserted_events = await self._batch_upsert_events(integration, events_to_upsert)
            existing.update(upserted_events)
            changed_event_ids = [str(event.event_id) for event in upserted_events.values()]
            changed_event_ids.extend(directly_changed_event_ids)

            # Reconcile each input item back to the row that came out of
            # RETURNING. A missing row here means the upsert silently dropped
            # the event — that's a data-integrity bug we want to fail loudly
            # on rather than silently lose, so we collect samples and raise.
            missing_upserted_events: list[dict[str, Any]] = []
            for item in events_to_upsert:
                identity = item.event.identity
                if not identity.external_id:
                    continue
                event = upserted_events.get((identity.external_id, identity.occurrence_anchor))
                if event is None:
                    missing_upserted_events.append(item.event.debug_sample)
                    continue

                self._queue_recording_dispatch_if_eligible(event)
                attendee_snapshots.append((event, item.event))
                user_external_id = item.event.user_external_id or identity.external_id
                user_states_to_upsert.append(
                    self._build_user_state_row(
                        event.event_id,
                        integration.integration_id,
                        user_external_id,
                        item.event.series_external_id,
                    )
                )

            if missing_upserted_events:
                logger.error(
                    "Calendar batch event upsert returned missing rows: integration=%s samples=%s",
                    integration.integration_id,
                    missing_upserted_events[:SAMPLE_LOG_LIMIT],
                )
                raise RuntimeError("Calendar batch event upsert returned missing rows")

            await self._batch_sync_attendees(attendee_snapshots)
            await self._batch_upsert_user_states(user_states_to_upsert)

            await self.db.commit()
            await self._dispatch_pending_recording_events()

            # --- Step 4: counter reconciliation log line ---
            # `unaccounted` should always be 0 — every input event must land in
            # exactly one bucket. A non-zero value means a routing branch is
            # missing a counter increment; investigate before changing the log.
            accounted = (
                created_count
                + updated_count
                + skipped_count
                + cancelled_existing_count
                + cancelled_no_record_count
                + deleted_processed_count
                + deleted_no_state_count
                + records_failed
            )
            unaccounted = len(event_list) - accounted

            logger.info(
                "Calendar incremental sync breakdown: integration=%s total=%d "
                "created=%d updated=%d unchanged=%d "
                "cancelled_existing=%d cancelled_no_record=%d "
                "deleted_processed=%d deleted_no_state=%d "
                "failed=%d unaccounted=%d "
                "existing_db_rows=%d",
                integration.integration_id,
                len(event_list),
                created_count,
                updated_count,
                skipped_count,
                cancelled_existing_count,
                cancelled_no_record_count,
                deleted_processed_count,
                deleted_no_state_count,
                records_failed,
                unaccounted,
                len(existing),
            )

            if missing_identity_samples:
                logger.info(
                    "Calendar sync sample missing-identity events: integration=%s samples=%s",
                    integration.integration_id,
                    missing_identity_samples,
                )
            if unchanged_samples:
                logger.info(
                    "Calendar sync sample unchanged events: integration=%s samples=%s",
                    integration.integration_id,
                    unchanged_samples,
                )

            # --- Step 5: close out sync history and integration status ---
            await self._sync_history.complete(
                sync_history,
                status=SyncStatus.SUCCESS,
                records_processed=records_processed,
                records_failed=records_failed,
                metadata={
                    "created": created_count,
                    "updated": updated_count,
                    "skipped": skipped_count,
                    "cancelled_existing": cancelled_existing_count,
                    "cancelled_no_record": cancelled_no_record_count,
                    "deleted_processed": deleted_processed_count,
                    "deleted_no_state": deleted_no_state_count,
                    "provider": provider,
                    "action_counts": action_counts.to_dict(),
                },
            )

            integration.last_synced_at = datetime_now_naive()
            integration.last_sync_status = SyncStatus.SUCCESS.value
            self.db.add(integration)
            await self.db.commit()

            return {
                "status": "success",
                "records_processed": records_processed,
                "records_failed": records_failed,
                "created": created_count,
                "updated": updated_count,
                "skipped": skipped_count,
                "cancelled_existing": cancelled_existing_count,
                "cancelled_no_record": cancelled_no_record_count,
                "deleted_processed": deleted_processed_count,
                "deleted_no_state": deleted_no_state_count,
                "changed_event_ids": list(dict.fromkeys(changed_event_ids)),
                "action_counts": action_counts.to_dict(),
            }

        except Exception as e:
            logger.error(
                "Calendar incremental sync failed: integration=%s error=%s",
                integration.integration_id,
                str(e),
                exc_info=True,
            )

            # Same fatal-failure handling as the full-sync path: roll the
            # integration to FAILED, truncate the error message to fit the
            # column, and re-raise so the worker can retry on its own schedule.
            await self._sync_history.complete(
                sync_history,
                status=SyncStatus.FAILED,
                records_processed=0,
                records_failed=len(event_list),
                error_details={"error": str(e), "error_type": type(e).__name__},
            )

            integration.last_sync_status = SyncStatus.FAILED.value
            integration.last_sync_error = str(e)[:500]
            self.db.add(integration)
            await self.db.commit()

            raise

    # The methods below are thin pass-throughs to the writer or the normalizer.
    # They exist so tests can monkeypatch one boundary at a time, and to keep
    # the orchestration code above readable (it talks to "self._upsert_event"
    # rather than knowing which writer method to call).

    async def _upsert_event(
        self,
        integration: KnowledgeIntegration,
        event: NormalizedCalendarEvent,
    ) -> CalendarEvent:
        return await self._writer.upsert_event(integration, event)

    async def _upsert_user_state(
        self,
        event: CalendarEvent,
        integration: KnowledgeIntegration,
        external_id: str,
        series_external_id: str | None = None,
    ) -> CalendarEventUserState:
        return await self._writer.upsert_user_state(
            event,
            integration,
            external_id,
            series_external_id,
        )

    async def _sync_attendees(
        self,
        event: CalendarEvent,
        normalized: NormalizedCalendarEvent,
    ) -> None:
        await self._writer.sync_attendees(event, normalized)

    async def _batch_upsert_events(
        self,
        integration: KnowledgeIntegration,
        items: list[CalendarEventUpsertBatchItem],
    ) -> dict[tuple[str, str], CalendarEvent]:
        return await self._writer.batch_upsert_events(integration, items)

    async def _batch_sync_attendees(
        self,
        event_snapshots: list[tuple[CalendarEvent, NormalizedCalendarEvent]],
    ) -> None:
        await self._writer.batch_sync_attendees(event_snapshots)

    async def _batch_upsert_user_states(
        self,
        rows: list[tuple[UUID, UUID, str, str | None]],
    ) -> None:
        await self._writer.batch_upsert_user_states(rows)

    def _queue_recording_dispatch_if_eligible(self, event: CalendarEvent) -> None:
        """Record changed future meetings that can be safely enqueued after commit."""
        start_at = normalize_utc(event.start_at)
        now = datetime.now(UTC)
        if start_at <= now:
            return
        if (getattr(event, "status", None) or "").lower() == "cancelled":
            # Cancellations still need the dispatch task: it re-reads the event and
            # cancels any scheduled Recall bot for this calendar event.
            self._pending_recording_event_ids.add(event.event_id)
            return
        if not is_within_recording_dispatch_window(start_at, now):
            return
        if not getattr(event, "conference_link", None):
            return
        self._pending_recording_event_ids.add(event.event_id)

    async def _dispatch_pending_recording_events(self) -> None:
        """Enqueue single-event meeting-bot work after calendar writes commit."""
        if not self._pending_recording_event_ids:
            return

        event_ids = sorted(self._pending_recording_event_ids, key=str)
        self._pending_recording_event_ids.clear()
        for event_id in event_ids:
            try:
                # Enqueue only after the calendar transaction commits. The task
                # re-reads the event, so dispatching earlier could observe stale
                # start time, cancellation status, or conference link.
                await send_deal_task_async(
                    "deal.send_meeting_recording",
                    kwargs={
                        "organisation_id": str(self.organisation_id),
                        "calendar_event_id": str(event_id),
                    },
                    queue="deal",
                    routing_key="deal",
                )
            except Exception:
                logger.error(
                    "Failed to dispatch meeting recording send task after calendar sync",
                    extra={
                        "event": "calendar_sync_meeting_recording_dispatch_failed",
                        "organisation_id": str(self.organisation_id),
                        "calendar_event_id": str(event_id),
                    },
                    exc_info=True,
                )

    @staticmethod
    def _build_user_state_row(
        event_id: UUID,
        integration_id: UUID,
        external_id: str,
        series_external_id: str | None,
    ) -> tuple[UUID, UUID, str, str | None]:
        return CalendarSyncWriter.build_user_state_row(
            event_id,
            integration_id,
            external_id,
            series_external_id,
        )

    def _has_meaningful_event_changes(
        self,
        existing: CalendarEvent,
        incoming: NormalizedCalendarEvent,
    ) -> bool:
        return has_meaningful_event_changes(existing, incoming)

    @staticmethod
    def _build_event_debug_sample(
        event_data: dict[str, Any],
        *,
        provider: str,
        external_id: str | None,
        occurrence_anchor: str,
    ) -> dict[str, Any]:
        return CalendarEventNormalizer.build_debug_sample(
            event_data,
            provider=provider,
            external_id=external_id,
            occurrence_anchor=occurrence_anchor,
        )

    @classmethod
    def _extract_identity(
        cls,
        event_data: dict[str, Any],
        provider: str,
    ) -> tuple[str | None, str, datetime | None]:
        return CalendarEventNormalizer.extract_identity(event_data, provider)

    @staticmethod
    def _extract_user_external_id(event_data: dict[str, Any], provider: str) -> str | None:
        return CalendarEventNormalizer.extract_user_external_id(event_data, provider)

    @staticmethod
    def _is_google_cancelled_exception(event_data: dict[str, Any], provider: str) -> bool:
        return CalendarEventNormalizer.is_google_cancelled_exception(event_data, provider)

    @staticmethod
    def _extract_event_fields(event_data: dict[str, Any], provider: str) -> dict[str, Any]:
        return CalendarEventNormalizer.extract_event_fields(event_data, provider)

    async def _find_event_by_user_external_id(
        self,
        user_external_id: str,
        integration_id: UUID,
    ) -> CalendarEvent | None:
        """Look up a calendar event via user_states using the per-mailbox/calendar ID.

        Used by the deletion and Google-cancellation branches in incremental
        sync. The org filter on `CalendarEvent.organisation_id` is the trust
        boundary — `user_external_id` is supplied by the provider and we never
        trust it on its own to surface rows across tenants.
        """
        stmt = (
            select(CalendarEvent)
            .join(
                CalendarEventUserState,
                col(CalendarEventUserState.event_id) == col(CalendarEvent.event_id),
            )
            .where(
                col(CalendarEventUserState.external_id) == user_external_id,
                col(CalendarEventUserState.integration_id) == integration_id,
                col(CalendarEvent.organisation_id) == self.organisation_id,
            )
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def _find_events_by_series_external_id(
        self,
        series_external_id: str,
        integration_id: UUID,
    ) -> list[tuple[CalendarEvent, str]]:
        """Load active occurrence rows for a provider recurring series tombstone."""
        stmt = (
            select(CalendarEvent, CalendarEventUserState.external_id)
            .join(
                CalendarEventUserState,
                col(CalendarEventUserState.event_id) == col(CalendarEvent.event_id),
            )
            .where(
                col(CalendarEvent.organisation_id) == self.organisation_id,
                col(CalendarEventUserState.integration_id) == integration_id,
                col(CalendarEventUserState.series_external_id) == series_external_id,
                col(CalendarEventUserState.is_deleted).is_(False),
            )
        )
        result = await self.db.execute(stmt)
        return [(event, external_id) for event, external_id in result.all()]

    async def _find_event_by_series_external_id_and_occurrence_anchor(
        self,
        series_external_id: str,
        occurrence_anchor: str,
        integration_id: UUID,
    ) -> tuple[CalendarEvent, str] | None:
        """Resolve a cancelled recurring exception by series id plus occurrence anchor."""
        stmt = (
            select(CalendarEvent, CalendarEventUserState.external_id)
            .join(
                CalendarEventUserState,
                col(CalendarEventUserState.event_id) == col(CalendarEvent.event_id),
            )
            .where(
                col(CalendarEvent.organisation_id) == self.organisation_id,
                col(CalendarEvent.occurrence_anchor) == occurrence_anchor,
                col(CalendarEventUserState.integration_id) == integration_id,
                col(CalendarEventUserState.series_external_id) == series_external_id,
                col(CalendarEventUserState.is_deleted).is_(False),
            )
        )
        result = await self.db.execute(stmt)
        row = result.first()
        if row is None:
            return None

        event, external_id = row
        return (event, external_id)

    async def _mark_user_state_records_deleted(
        self,
        records: list[tuple[CalendarEvent, str]],
        integration_id: UUID,
        directly_changed_event_ids: list[str],
    ) -> bool:
        deleted_any = False
        for record, user_external_id in records:
            deleted = await self._mark_user_state_deleted(
                record.event_id,
                integration_id,
                user_external_id,
            )
            if not deleted:
                continue

            deleted_any = True
            if await self._all_user_states_deleted(record.event_id):
                record.status = "cancelled"
                record.synced_at = datetime.now(UTC)
                self.db.add(record)
                directly_changed_event_ids.append(str(record.event_id))
                self._queue_recording_dispatch_if_eligible(record)

        return deleted_any

    async def _mark_user_state_deleted(
        self,
        event_id: UUID,
        integration_id: UUID,
        user_external_id: str,
    ) -> bool:
        """Soft-delete one (event, integration) user-state row.

        Returns True when the row was found and flipped, False when no such row
        existed. Caller uses the False return to bump the
        `deleted_no_state` / `cancelled_no_record` counter.
        """
        stmt = select(CalendarEventUserState).where(
            col(CalendarEventUserState.event_id) == event_id,
            col(CalendarEventUserState.integration_id) == integration_id,
            col(CalendarEventUserState.external_id) == user_external_id,
        )
        result = await self.db.execute(stmt)
        state = result.scalar_one_or_none()
        if state is None:
            return False

        state.is_deleted = True
        state.updated_at = datetime.now(UTC)
        self.db.add(state)
        return True

    async def _all_user_states_deleted(self, event_id: UUID) -> bool:
        """Return True when no integration still considers this event active.

        Caller uses this to decide whether to flip the shared event row's
        status to `cancelled`. We only cancel the shared row when the last
        active integration drops out — otherwise one attendee deleting their
        copy would silently cancel the meeting for the rest of the org.
        """
        stmt = select(CalendarEventUserState).where(
            col(CalendarEventUserState.event_id) == event_id,
            col(CalendarEventUserState.is_deleted).is_(False),
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none() is None

    async def _load_existing_events_by_identity(
        self,
        identity_pairs: list[tuple[str, str]],
    ) -> dict[tuple[str, str], CalendarEvent]:
        """Load existing calendar events by shared UID and occurrence anchor.

        One round trip for the whole batch. The org filter on
        `organisation_id` is the trust boundary — identity pairs are derived
        from provider payloads we don't fully trust on their own.
        """
        if not identity_pairs:
            return {}

        stmt = select(CalendarEvent).where(
            col(CalendarEvent.organisation_id) == self.organisation_id,
            tuple_(col(CalendarEvent.external_id), col(CalendarEvent.occurrence_anchor)).in_(
                identity_pairs
            ),
        )
        result = await self.db.execute(stmt)
        events = result.scalars().all()

        return {(e.external_id, e.occurrence_anchor): e for e in events}
