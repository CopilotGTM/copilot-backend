# Calendar Maintenance Scripts

Scripts for calendar event data migration, cleanup, and sync management. All scripts default to **dry run** — use `--execute` to persist changes.

## Scripts

### `deduplicate_calendar_events.py`
Merges duplicate calendar event rows caused by the old identity model (mailbox-specific IDs instead of shared UIDs). Finds duplicates by reading `data_payload` for shared UID (`iCalUID` for Google, `iCalUId` for Outlook), picks the richest row as winner (based on meetings, transcripts, deal associations), merges child records onto the winner, and deletes losers. Also backfills identity columns (`external_id`, `provider`, `occurrence_anchor`, `original_start_at`) if the migration has been applied.

```powershell
# Dry run for one org
.venv\Scripts\python.exe -m scripts.maintenance.calendar.deduplicate_calendar_events --org-id "<org-id>"

# Execute
.venv\Scripts\python.exe -m scripts.maintenance.calendar.deduplicate_calendar_events --org-id "<org-id>" --execute
```

### `hydrate_missing_icaluid.py`
Fetches full event details from provider APIs (Google Calendar API / Microsoft Graph) for events where `data_payload` is missing the shared UID. Updates `data_payload` with the full response and sets `external_id` to the shared UID. Supports both Google and Outlook calendars.

```powershell
.venv\Scripts\python.exe -m scripts.maintenance.calendar.hydrate_missing_icaluid --org-id "<org-id>"
.venv\Scripts\python.exe -m scripts.maintenance.calendar.hydrate_missing_icaluid --org-id "<org-id>" --execute
```

### `trigger_initial_integrations_sync.py`
Resets sync cursors (delta tokens, sync tokens, last_synced_at) and dispatches manual integration sync tasks. Supports `--family` flag to limit to specific integration types (calendar, email, messaging, crm).

```powershell
# Calendar only
.venv\Scripts\python.exe -m scripts.maintenance.calendar.trigger_initial_integrations_sync --family calendar --org-id "<org-id>" --execute

# All families (default)
.venv\Scripts\python.exe -m scripts.maintenance.calendar.trigger_initial_integrations_sync --execute
```

### `delete_series_calendar_events.py`
Deletes recurring/series calendar events and all dependent records (user_states, attendees, deal_associations, meetings, transcript_segments, deal_jobs). Used for clean re-fetch of recurring data after identity model changes.

```powershell
.venv\Scripts\python.exe -m scripts.maintenance.calendar.delete_series_calendar_events --org-id "<org-id>"
.venv\Scripts\python.exe -m scripts.maintenance.calendar.delete_series_calendar_events --org-id "<org-id>" --execute
```

### `delete_today_calendar_events.py`
Emergency cleanup script. Deletes calendar events created on a specific date and all dependent records. Old meetings (created before the target date) are unlinked instead of deleted to preserve transcript data.

```powershell
# Defaults to today
.venv\Scripts\python.exe -m scripts.maintenance.calendar.delete_today_calendar_events --org-id "<org-id>"

# Specific date
.venv\Scripts\python.exe -m scripts.maintenance.calendar.delete_today_calendar_events --org-id "<org-id>" --date 2026-03-25 --execute
```

### `clear_non_google_calendar_occurrence_fields.py`
Clears stale occurrence anchor fields when the payload has no supported recurring-anchor field. Do not use to flatten all Outlook anchors — that causes unique-key collisions.

```powershell
.venv\Scripts\python.exe -m scripts.maintenance.calendar.clear_non_google_calendar_occurrence_fields --org-id "<org-id>"
```

## Deployment Order

1. `deduplicate_calendar_events.py --execute` — merge duplicates (works on current schema)
2. Run Alembic org migration 037 — adds identity columns + unique constraint
3. `deduplicate_calendar_events.py --execute` — backfills identity columns
4. `hydrate_missing_icaluid.py --execute` — fetches missing shared UIDs from provider APIs
5. `trigger_initial_integrations_sync.py --family calendar --execute` — fresh sync with new identity model
