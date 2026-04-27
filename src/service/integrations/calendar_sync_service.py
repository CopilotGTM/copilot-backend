"""Compatibility shim — re-exports calendar sync types for legacy imports.

The real implementation lives in `src.service.integrations.calendar`. This
module exists only because older callers (a couple of worker tasks and existing
test files) still import `CalendarSyncService` and `CalendarEventUpsertBatchItem`
from this old path. Routing them through this re-export keeps the migration
window safe — we can update import sites incrementally without breaking
anything that hasn't been migrated yet.

Remove this file (and the TODO below) once every import has been moved over to
`src.service.integrations.calendar`.
"""

# TODO(calendar-sync): Remove this shim after all callers import from
# src.service.integrations.calendar directly.
from src.service.integrations.calendar import CalendarEventUpsertBatchItem, CalendarSyncService

__all__ = ["CalendarEventUpsertBatchItem", "CalendarSyncService"]
