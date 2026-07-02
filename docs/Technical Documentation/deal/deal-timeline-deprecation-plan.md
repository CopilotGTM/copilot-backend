# Deal Timeline Deprecation Plan

The frontend timeline module has been removed. The backend legacy timeline
surface should move out of the live path instead of continuing to write, route,
or schedule work for `deal_timeline_events`.

This document captures the current backend surface before the cleanup. The
target is to deprecate the legacy `DealTimelineEvent` feature while preserving
the newer CRM activity feed, deal matching, risks/blockers, stakeholders, and
activity projection flows.

## Current State

### Runtime table and model

- `migrations/org_migrations/versions/3c7f79637780_020_deal_timeline_events.py`
  created `deal_timeline_events`.
- `src/domain/models/organisation_tables/sync/timeline_models.py` defines:
  - `DealTimelineEvent`
  - `TimelineEventType`
  - `TimelineEventSource`
- `migrations/org_migrations/env.py` still imports `DealTimelineEvent` for
  metadata discovery.

Do not drop the table in this cleanup unless product explicitly approves data
removal. The first lazy step is to stop live writes, routes, and jobs; schema
removal can be a separate migration after retention/export requirements are
clear.

### API surface

`src/api/fastapi_app.py` still mounts `deal_timeline_router` at
`/users/crm/deals`.

`src/api/routers/crm/deal_timeline_handler.py` contains the legacy timeline
surface:

- Legacy timeline endpoints:
  - `GET /users/crm/deals/{deal_id}/timeline`
  - `GET /users/crm/deals/timeline/{timeline_event_id}`

Do not create a replacement deal activity handler in this cleanup. Break the
legacy timeline route link here and leave activity-feed routing for the later
activity-feed handler work.

### Services

Live legacy package:

- `src/service/crm/timeline/deal_timeline_service.py`
  - Reads and enriches `DealTimelineEvent` records.
  - Supports list/detail reads, exclusion, and direct email/meeting event
    creation helpers.
- `src/service/crm/timeline/deal_timeline_sync_service.py`
  - Batch backfill/reconciliation service.
  - Also hosts builder functions used by inline writers:
    - `build_email_timeline_event`
    - `build_meeting_timeline_event`
    - `build_stage_change_timeline_event`
    - `build_deal_created_timeline_event`
    - `build_risk_blocker_timeline_event`
    - `build_stakeholder_timeline_event`
- `src/service/crm/timeline/timeline_event_source_resolver.py`
  - Resolves provider/source enum values for legacy timeline cards.
- `src/service/crm/__init__.py` re-exports timeline services/builders.

Already deprecated but still referencing timeline builders:

- `deprecated/service/crm_sync/entities/deal_sync_service.py`

### Worker tasks and triggers

The scheduled `timeline-sync` CronJob is already disabled:

- `src/workers/deal_worker/scheduled_jobs.py` has no `timeline-sync` entry.
- `tests/workers/deal_worker/test_cron_dispatch.py` asserts
  `get_scheduled_job("timeline-sync")` raises `KeyError`.
- `charts/application/values*.yaml` contain no deal CronJob named
  `timeline-sync`.
- `tests/charts/test_worker_cronjobs.py` asserts
  `copilot-backend-timeline-sync` is not rendered.

Remaining live worker surface:

- `src/workers/deal_worker/tasks/deal_mappers/timeline_sync_tasks.py`
  registers:
  - `deal.trigger_timeline_sync_all_orgs`
  - `deal.sync_org_timeline`
  - `deal.sync_deal_timeline`
  - `deal.exclude_timeline_event`
- `src/workers/deal_worker/config.py` still routes those task names to the
  `deal` queue.
- `src/workers/deal_worker/tasks/__init__.py` and
  `src/workers/deal_worker/tasks/deal_mappers/__init__.py` still export those
  task objects.
- `src/workers/deal_worker/tasks/deal_mappers/email_matching_tasks.py`
  dispatches `sync_deal_timeline_task` after deal-level email rematching.
- `src/workers/deal_worker/tasks/deal_mappers/calendar_matching_tasks.py`
  dispatches `sync_deal_timeline_task` after deal-level calendar rematching.
- `src/workers/deal_worker/README.md` still documents
  `deal.trigger_timeline_sync_all_orgs` as a 15-minute reconciliation job, which
  is stale relative to the schedule registry and chart values.

### Inline writers

Timeline writes still happen even without the scheduled job:

- `src/workers/deal_worker/services/deal_mapper_services/deal_email_matcher.py`
  builds and adds `DealTimelineEvent` rows when email associations are created.
- `src/workers/deal_worker/services/deal_mapper_services/deal_meeting_matcher.py`
  builds and adds `DealTimelineEvent` rows when meeting associations are
  created.
- `src/service/crm/risks/write_service.py` adds opened risk/blocker timeline
  events.
- `src/service/crm/risks/lifecycle_service.py` adds resolved/dismissed
  blocker timeline events.
- `src/workers/deal_worker/services/deal_insight_services/risk_blocker_extraction_service.py`
  creates extracted items through `RiskBlockerWriteService`, which now projects
  activity-feed rows instead of writing timeline events.

These are the highest-value cleanup points after API route removal because they
keep generating data for a frontend surface that no longer exists.

### Tests

Timeline-specific tests should move with the deprecated code or be deleted once
the live imports are removed:

- `tests/service/test_timeline_event_source_resolver.py`
- `tests/service/crm/test_deal_timeline_sync_service.py`
- `tests/service/crm/test_deal_timeline_service_participants.py`
- `tests/workers/deal_worker/tasks/deal_mappers/test_timeline_sync_tasks.py`

Tests that should be updated, not blindly deleted:

- `tests/workers/deal_worker/tasks/deal_mappers/test_email_matching_tasks.py`
  currently asserts timeline sync dispatch after deal-level email rematch.
- `tests/workers/deal_worker/tasks/deal_mappers/test_calendar_matching_tasks.py`
  currently asserts timeline sync dispatch after deal-level calendar rematch.
- Risk/blocker tests that assert `TimelineEventType` should be rewritten around
  durable risk/blocker behavior, or moved with the deprecated timeline writer
  assertions if the only assertion is legacy event creation.
- `tests/service/crm/test_activity_feed_read_service.py` should stay live; it
  already asserts the activity feed does not read `deal_timeline_events`.

### Existing documentation to update or deprecate

- `docs/Technical Documentation/deal/deal-timeline.md`
- `docs/Technical Documentation/deal_mappings/deal-timeline-complete-flow.md`
- `docs/API Documentation/deals/deal-timeline-api.md`
- `docs/Technical Documentation/deal/task-orchestration-map.md`
- `src/workers/deal_worker/README.md`

The API documentation is now stale because the frontend timeline module is gone.
The activity-feed endpoint can get a smaller replacement doc if it does not
already have one elsewhere.

## Target State

1. No live API route exposes legacy `deal_timeline_events`.
2. No new deal activity-feed handler is introduced during timeline deprecation.
3. No live Celery task registers or routes `deal.*timeline*` task names.
4. Email/calendar matching no longer dispatches timeline sync after rematch.
5. Inline email, meeting, risk, blocker, stakeholder, and CRM sync paths do not
   create `DealTimelineEvent` rows.
6. Legacy timeline service, sync service, route, schemas, metadata config, and
   narrow tests are moved under `deprecated/` or removed from live imports.
7. Current migrations keep historical tables available unless a separate
   approved schema cleanup decides to drop them.

## Cleanup Order

1. Stop mounting legacy timeline endpoints and move the legacy handler/schemas
   to `deprecated/`.
2. Remove timeline task exports, worker routes, and the whole
   `timeline_sync_tasks.py` live module.
3. Remove email/calendar rematch dispatch to `sync_deal_timeline_task` and
   update their tests/results.
4. Remove inline `DealTimelineEvent` creation from email, meeting, risk/blocker,
   and stakeholder/CRM write paths.
5. Move `src/service/crm/timeline/` to `deprecated/service/crm/timeline/` after
   no live imports remain.
6. Move or delete timeline-only tests. Keep one small guard that proves no
   `deal.*timeline*` tasks are registered/routed and no timeline API routes are
   mounted.
7. Update stale docs listed above.

## Verification

Minimum checks for the implementation PR:

```powershell
rg -n "deal_timeline|DealTimeline|deal\\.sync_.*timeline|trigger_timeline|timeline-sync|deal_timeline_events" src tests charts docs
uv run pytest tests/workers/deal_worker/test_cron_dispatch.py tests/charts/test_worker_cronjobs.py
```

Expected search result after cleanup: live `src/` should have no legacy timeline
service, task, route, or write imports. Remaining matches should be limited to
`deprecated/`, migrations, and explicit historical/deprecation docs.
