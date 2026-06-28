# Calendar Recurring Deletion Reconciliation Issue

Date: 2026-06-27

Worktree: `D:\CopilotGTM\copilot-backend-webhook-enhancement`

Branch: `codex/webhook-enhancement`

## Summary

Calendar deletion reconciliation is too narrow for recurring-series deletes from Google and
Microsoft. The current incremental sync path can correctly process a delete when the provider
deleted id exactly matches `calendar_event_user_states.external_id`, but it does not reconcile a
series/master tombstone to locally stored occurrence rows.

The failure shape is:

1. A recurring meeting series is synced into local `calendar_events` rows as expanded occurrences.
2. The provider later sends a delete/cancel payload for the recurring series/master id.
3. The local `CalendarEventUserState.external_id` values may be occurrence ids, not the
   series/master id.
4. The exact user-state lookup misses.
5. The future occurrence rows stay active, `calendar_events.status` is not set to `cancelled`, and
   Recall cancellation reconciliation is not queued for those occurrences.

## Current Data Model

The local calendar model has two relevant layers:

- `calendar_events` is the canonical event/occurrence row. It is keyed by
  `(external_id, occurrence_anchor)` and stores the raw provider payload in `data_payload`.
- `calendar_event_user_states` is the per-integration visibility row. It stores the mailbox-specific
  provider event id in `external_id` and tracks whether that mailbox copy was deleted with
  `is_deleted`.

Relevant evidence:

- `CalendarEvent` has `external_id`, `occurrence_anchor`, `data_payload`, and `status` fields in
  `src/domain/models/organisation_tables/sync/calendar.py`.
- `CalendarEventUserState` has `event_id`, `integration_id`, `external_id`, and `is_deleted` in the
  same file.
- The normalizer keeps provider recurrence identifiers in diagnostic/raw payload paths:
  Google `recurringEventId` and Microsoft `seriesMasterId`.

Important correction from code review:

- `calendar_events.data_payload` is on the shared canonical row, not the per-integration state row.
  Multiple users' calendars can contribute to the same canonical event. The last writer can replace
  `data_payload` with that integration's provider payload.
- Provider series/master ids are mailbox/provider-copy identities. They must not be treated as
  canonical event identities unless we only use them for diagnostics.
- A production-safe delete resolver should store the provider series/master id on
  `calendar_event_user_states`, because that row is already scoped to the current integration's
  mailbox copy.

## Current Code Path

The incremental sync delete path starts in
`src/service/integrations/calendar/sync_service.py`.

For explicit provider deletes, the code currently:

1. Extracts the per-user external id from the provider payload.
2. Calls `_find_event_by_user_external_id(user_external_id, integration_id)`.
3. If found, calls `_mark_user_state_deleted(event_id, integration_id, user_external_id)`.
4. Cancels the canonical event only when `_all_user_states_deleted(event_id)` returns true.
5. Queues meeting recording dispatch for cancelled future events.

That logic is correct for one-off event deletes and for recurrence payloads whose deleted id matches
the stored user-state id. It is not sufficient when the provider sends the recurring series/master
id while the local user states contain occurrence ids.

The same narrow lookup is also used by the Google cancelled-event branch. That branch handles
standalone Google cancellation payloads without a shared UID, but it still depends on an exact
`CalendarEventUserState.external_id == user_external_id` match before marking the per-user state
deleted.

## Root Cause

The root cause is an identity-shape mismatch between provider deletion payloads and local occurrence
storage.

Provider deletion payload:

- Google can send the deleted series/master id.
- Microsoft can send the deleted series master id.
- Google can also send a cancelled single occurrence with `recurringEventId` +
  `originalStartTime` and no `iCalUID`.

Local stored occurrence rows:

- Google occurrence rows retain provider payloads that can include `recurringEventId`.
- Microsoft occurrence rows retain provider payloads that can include `seriesMasterId`.
- `CalendarEventUserState.external_id` may be the occurrence id, not the series/master id.

The old/current reconciliation assumes the delete payload id is always the exact per-mailbox event
id stored in `calendar_event_user_states.external_id`. That assumption breaks for recurring-series
tombstones.

The first draft solution proposed querying `CalendarEvent.data_payload` for `recurringEventId` /
`seriesMasterId`. That is useful as a transitional/backfill fallback, but it is not the clean
production model because `data_payload` is not per-integration. The normalized series id needs its
own per-user-state column.

## User Impact

When the miss happens:

- The integration's user-state rows for future occurrences remain `is_deleted = false`.
- The shared `calendar_events.status` remains active/confirmed.
- Meeting-list and downstream matching logic can continue treating deleted future occurrences as
  active.
- Recall bot cancellation is not triggered, because the sync service only queues cancellation work
  when it actually flips a canonical event to `cancelled`.

Recall impact is important because `MeetingRecordingDispatchService` treats a cancelled calendar
event as actionable and cancels any scheduled bot for that event. If the canonical event never
becomes cancelled, that reconciliation path is never reached.

## Missing Test Coverage

Existing tests cover:

- Mixed incremental create/update/unchanged/cancelled flow.
- Cancelled future events enqueue meeting-bot reconciliation after commit.
- Basic detection that Google recurring instances use `recurringEventId` and Microsoft recurring
  instances use `seriesMasterId`.

Missing regression tests:

1. Google series tombstone fan-out:
   - Existing user-state rows have `series_external_id == deleted_id`.
   - Existing user states have occurrence external ids, not the deleted series id.
   - A `SyncAction.DELETED` payload arrives with `id == deleted_id`.
   - The service marks all matching states for that integration deleted.
   - It cancels each canonical occurrence only when all active user states for that occurrence are
     deleted.
   - It queues Recall reconciliation for each newly cancelled future occurrence.

2. Microsoft series tombstone fan-out:
   - Existing user-state rows have `series_external_id == deleted_id`.
   - Existing user states have occurrence external ids.
   - A delete payload arrives with `id == deleted_id`.
   - The same state deletion, canonical cancellation, and Recall reconciliation behavior is
     verified.

3. Google cancelled exception consistency:
   - A Google cancelled recurring exception can arrive without `iCalUID`.
   - It should match either exact `CalendarEventUserState.external_id` or
     `(CalendarEventUserState.series_external_id, CalendarEvent.occurrence_anchor)`.
   - It should mark the matching user-state row deleted and only cancel the canonical event when all
     active user states for that event are deleted.

4. Tenant boundary:
   - Series fallback queries must include `CalendarEvent.organisation_id == organisation_id`.
   - They must also scope user-state writes to the current `integration_id`.

## Proposed Fix Contract

Production solution:

1. Add a nullable `series_external_id` column to `calendar_event_user_states`.
   - Google value: raw event `recurringEventId`.
   - Microsoft value: raw event `seriesMasterId`.
   - Non-recurring events: `NULL`.
2. Teach `CalendarEventNormalizer` to expose this provider-copy series id on
   `NormalizedCalendarEvent`.
3. Teach `CalendarSyncWriter` to upsert `series_external_id` alongside
   `CalendarEventUserState.external_id`.
4. Keep exact delete lookup by `CalendarEventUserState.external_id` as the first path. It is cheap
   and correct for normal event deletes and single occurrence deletes whose tombstone id is the
   occurrence id.
5. If exact lookup misses, treat the provider deleted id as a possible recurring series/master
   tombstone and resolve rows by:
   - `CalendarEvent.organisation_id == organisation_id`
   - `CalendarEventUserState.integration_id == current integration_id`
   - `CalendarEventUserState.series_external_id == deleted_provider_id`
   - `CalendarEventUserState.is_deleted == false`
6. For Google cancelled single-occurrence exceptions without `iCalUID`, resolve by:
   - exact `CalendarEventUserState.external_id`, when available; otherwise
   - `series_external_id == recurringEventId` plus `CalendarEvent.occurrence_anchor` derived from
     `originalStartTime`.
7. For each affected event, mark the current integration's user-state row deleted.
8. Only set `CalendarEvent.status = "cancelled"` when no active user states remain for that event.
9. Queue Recall reconciliation only for events that newly become cancelled.

Transitional fallback:

- For rows synced before `series_external_id` exists or before it was backfilled, a bounded fallback
  can still inspect `CalendarEvent.data_payload.recurringEventId` / `seriesMasterId`.
- That fallback should be treated as best-effort compatibility only, not the primary production
  identity model, because the canonical event payload is not per-integration.

Do not cancel a shared canonical event just because one user's mailbox copy was deleted. The
existing `all_user_states_deleted` gate is the right rule and should be reused.

## Implementation Notes

Likely code changes:

- Add an org migration for `calendar_event_user_states.series_external_id` plus an index on
  `(integration_id, series_external_id)` filtered or nullable-safe for recurring lookups.
- Add `series_external_id` to `NormalizedCalendarEvent`.
- Add `CalendarEventNormalizer.extract_series_external_id(...)`.
- Add the column to `CalendarSyncWriter.build_user_state_values(...)` and update the user-state
  upsert tuple/type.
- Add sync-service helpers that resolve:
  - exact user-state deletes,
  - series deletes,
  - Google single-occurrence cancelled exceptions without `iCalUID`.
- Keep the existing all-user-states-deleted gate as the shared cancellation rule.

Suggested focused verification:

```bash
uv run pytest tests/service/integrations/test_calendar_sync_service_incremental.py -v
uv run pytest tests/service/meeting_bot/test_meeting_recording_dispatch_service.py -v -k cancelled
```

## Open Questions

- Should the migration backfill `series_external_id` from the current `CalendarEvent.data_payload`
  for existing rows? This is imperfect because the payload is canonical/shared, but it may recover a
  useful subset of existing rows.
- Should whole-series deletes mark all locally stored occurrences for the integration, or only
  future occurrences? Data consistency argues for all matching rows; Recall dispatch already filters
  operationally.
