# Deal Worker

The **Deal Worker** is a Celery application dedicated to deal-centric background processing: matching meetings to CRM deals, syncing stakeholders, running AI workloads, and streaming results back to the product UI.

---

## Responsibilities at a Glance

- Match `CalendarEvent` records to `CRMDeal` entries and maintain associations.
- Discover, sync, and enrich deal stakeholders using attendee lists and CRM context.
- Map inbound/outbound emails to deals using participant + thread signals and refresh stakeholders from email touches.
- Execute AI-powered flows (meeting briefings/summaries, deal-level summaries, stakeholder analysis).
- Publish job progress/results to Redis Streams that back SSE endpoints.
- Run periodic housekeeping (meeting reconciliation, org-wide analysis) via Celery Beat.

---

## Repository Layout

```text
src/workers/deal_worker/
|-- __init__.py
|-- README.md
|-- celery_app.py              # Celery bootstrap + signals + monitoring hook
|-- config.py                  # Queue, routing, and beat schedule configuration
|-- services/
|   |-- deal_insight_services/ # Meeting-level AI services
|   |   |-- meeting_prep_service.py    # Meeting briefing generation
|   |   `-- meeting_summary_service.py # Meeting summary generation
|   |-- deal_job_publisher.py  # Redis Stream publisher for SSE updates
|   |-- deal_job_utils.py      # Shared helpers (context builders, fetchers)
|   |-- deal_meeting_matcher.py
|   |-- deal_summary_service.py
|   |-- stakeholder_analysis_service.py
|   `-- stakeholder_service.py
|-- tasks/
|   |-- __init__.py
|   |-- meeting_insight_tasks/ # Meeting-level AI tasks
|   |   |-- meeting_prep_task.py
|   |   `-- meeting_summary_task.py
|   |-- deal_mappers.py
|   |-- deal_summary_tasks.py
|   |-- stakeholder_analysis_tasks.py
|   `-- transcript_fetch_tasks.py
`-- utils/
```

---

## Core Components

- **`celery_app.py`** - sets DB pool env vars, loads `.env`, creates the Celery app via `create_celery_app`, registers worker lifecycle signal handlers, and wires in `scripts/monitoring/celery_integration.py` for metrics/log shipping.
- **`config.py`** - extends `BaseWorkerConfig`, defines the `deal`/`deal_jobs` queues, routing rules, and two Beat schedules.
- **Services**:
  - `deal_meeting_matcher.py` - links meetings to deals and triggers attendee/domain-based stakeholder discovery.
  - `stakeholder_service.py` - persists stakeholders discovered from CRM contacts and meeting attendees.
  - `stakeholder_analysis_service.py` - batches stakeholders per deal, builds transcript context, runs `StakeholderAnalysisAgent`, and writes stance/power insights back.
  - `deal_insight_services/` - Contains `MeetingPrepService` and `MeetingSummaryService` that power the AI-driven meeting briefings and summaries. Each follows the fresh session pattern for efficient DB connection management.
  - `deal_job_publisher.py` - publishes progress/results via Redis Streams for SSE consumption.
  - `deal_summary_service.py` - orchestrates deal-level transcript aggregation and calls `DealSummaryAgent`, persisting the output on `CRMDeal`.

---

## Queues and Routing

| Queue | Purpose |
|-------|---------|
| `deal` | Default queue for deterministic workloads: matching, stakeholder sync, periodic triggers. |
| `deal_jobs` | AI/LLM-heavy workloads (meeting jobs, stakeholder analysis) isolated for scaling. |

`DealWorkerConfig.task_routes` pins each task to the right queue. When adding new tasks, update the routes to avoid falling back to the default queue unintentionally.

---

## Task Inventory

### Deal Matching (`tasks/deal_tasks.py`)

| Task Name | Queue | Description |
|-----------|-------|-------------|
| `deal.process_deal_meetings_task` | `deal` | Reconciles meetings and deals + hydrates stakeholders for a single org. |
| `deal.process_single_deal_task` | `deal` | Scopes processing to one deal (used from UI/API actions). |
| `deal.trigger_deal_meeting_processing_all_orgs` | `deal` | Beat-triggered fan-out that enqueues `process_deal_meetings_task` for every approved org. |

### Meeting Insight Tasks (`tasks/meeting_insight_tasks/`)

| Task Name | Queue | Description |
|-----------|-------|-------------|
| `meeting_prep_task.process` | `deal_jobs` | Generates pre-meeting briefing via `MeetingPrepService`, streams progress/results to Redis/SSE. |
| `meeting_summary_task.process` | `deal_jobs` | Generates post-meeting summary via `MeetingSummaryService`, streams progress/results to Redis/SSE. |

### Deal Summary (`tasks/deal_summary_tasks.py`)

| Task Name | Queue | Description |
|-----------|-------|-------------|
| `deal_summary_task.process` | Default `deal` (override to `deal_jobs` when enqueueing) | Generates a holistic deal summary across all meetings via `DealSummaryService`, persists it on the `CRMDeal`, and streams the result. |

### Stakeholder Sync (`tasks/stakeholder_tasks.py`)

| Task Name | Queue | Description |
|-----------|-------|-------------|
| `stakeholder.sync_from_meeting_task` | `deal` | Syncs attendees from a specific meeting into deal stakeholders, enriches stance + domains, and persists the results. |

### Stakeholder Analysis (`tasks/stakeholder_analysis_tasks.py`)

| Task Name | Queue | Description |
|-----------|-------|-------------|
| `stakeholder.analyze_deal_stakeholders_task` | `deal_jobs` | Runs batch AI analysis for a single deal's stakeholders and writes stance/power/motivations back. |
| `stakeholder.analyze_org_stakeholders_task` | `deal_jobs` | Iterates all deals with stakeholders for an org, batching LLM calls per deal. |
| `stakeholder.trigger_analysis_all_orgs` | `deal` | Beat-triggered fan-out that enqueues org-wide analysis for every approved org. |

### Email Mapping (`tasks/email_tasks.py`)

| Task Name | Queue | Description |
|-----------|-------|-------------|
| `email.process_single_email_task` | `deal` | Map a single email to deals using participant + thread signals and update stakeholders. |
| `email.process_org_emails_task` | `deal` | Incremental sweep for one org using a cursor to avoid rescanning mailboxes. |
| `email.process_deal_emails_task` | `deal` | Backfill mapping for a single deal (e.g., after CRM contact updates). |
| `email.trigger_email_mapping_all_orgs` | `deal` | Beat-triggered fan-out that enqueues org sweeps hourly for all approved orgs. |

---

## Triggering Tasks Programmatically

### Using `send_task`

```python
from src.workers.deal_worker.celery_app import app as deal_worker_app

deal_worker_app.send_task(
    "deal.process_deal_meetings_task",
    args=[str(organisation_id)],
    queue="deal",
    routing_key="deal",
)

# AI-heavy workloads -> deal_jobs

deal_worker_app.send_task(
    "stakeholder.analyze_deal_stakeholders_task",
    args=[str(org_id), str(deal_id)],
    queue="deal_jobs",
    routing_key="deal_jobs",
)
```

### Importing the Task Function

```python
from src.workers.deal_worker.tasks.deal_tasks import process_deal_meetings_task

process_deal_meetings_task.delay(organisation_id="<org_uuid>")
```

When enqueueing meeting jobs, deal summaries, or stakeholder analysis, explicitly set `queue="deal_jobs"` to ensure they run on the worker pool provisioned for LLM workloads.

---

## Running the Worker

> Ensure the virtual environment is activated and the project root is the working directory.

### Development

```powershell
# Windows (PowerShell)
celery -A src.workers.deal_worker.celery_app worker -l info -Q deal,deal_jobs --pool=solo
```

```bash
# Linux / macOS
celery -A src.workers.deal_worker.celery_app worker --loglevel=info -Q deal,deal_jobs --pool=solo
```

### Celery Beat

```bash
celery -A src.workers.deal_worker.celery_app beat -l info
```

Run Beat alongside at least one worker (or as a dedicated process) so the scheduled tasks keep firing.

### Production Example

```bash
celery -A src.workers.deal_worker.celery_app worker -l warning -Q deal,deal_jobs --concurrency=4
```

Always listen to both queues; otherwise AI workloads or deterministic jobs will starve.

---

## Periodic Tasks

| Schedule | Task | Purpose |
|----------|------|---------|
| Every 15 minutes at `:03,:18,:33,:48` | `stakeholder.sync_all_deals_stakeholders_all_orgs` | Refresh stakeholder mappings and downstream matching for all approved organisations. |
| Every 15 minutes at `:10,:25,:40,:55` | `deal.trigger_timeline_sync_all_orgs` | Reconcile timeline gaps caused by missed inline writes. |
| Every 30 minutes at `:05,:35` | `deal.trigger_transcript_fetch_all_orgs` | Fetch transcripts and kick off the meeting/deal summary pipeline. |
| Hourly at `:20` | `stakeholder_research.trigger_retry_all_orgs` | Retry stakeholder enrichment that previously failed or never completed. |
| Hourly at `:45` | `deal.cleanup_stale_jobs_all_orgs` | Mark stale deal jobs as failed so they do not remain stuck indefinitely. |
| Hourly at `:50` | `deal.trigger_send_unsent_meeting_recordings_all_orgs` | Catch deal-linked upcoming meetings that were missed by the event-driven send path. |
| Every 3 hours at `HH:10` | `deal_memory.trigger_backfill_all_orgs` | Backfill deal memory for organisations that have source data but no memory state. |

Fixed schedules are triggered by Kubernetes CronJobs and dispatched through
`src.workers.deal_worker.cron_dispatch`; update `scheduled_jobs.py` and the
Helm `dealCronJobs` values together when adding or changing periodic jobs.

---

## Environment & Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `REDIS_HOST` / `REDIS_PORT` / `REDIS_PASSWORD` | Connection info for Redis Streams used by `DealJobRedisPublisher`. | `localhost` / `6379` / `None` |
| `MAX_TENANT_ENGINES` | Hard-set to `1` in `celery_app.py` before imports to cap tenant DB pools per worker. | `1` |
| `TENANT_POOL_MULTIPLIER` | Pool multiplier applied by the DB factory. | `1.0` |
| `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND` | Inherited from global Celery settings via `BaseWorkerConfig`. | See project `.env` |

The worker loads `.env` (via `dotenv`) during boot. Configure broker/backends and Redis credentials there.

---

## Monitoring & Debugging

```bash
# Active tasks
celery -A src.workers.deal_worker.celery_app inspect active

# Registered tasks
celery -A src.workers.deal_worker.celery_app inspect registered

# Reserved (queued) tasks
celery -A src.workers.deal_worker.celery_app inspect reserved

# Stream task lifecycle events
celery -A src.workers.deal_worker.celery_app events
```

- Worker lifecycle signals (`worker_ready`, `worker_shutdown`, etc.) initialize/tear down the Redis pool (`initialize_redis_pool` / `close_redis_pool`).
- `DealJobRedisPublisher` frames (`progress`, `answer.replace`, `error`, `finalize`) feed the SSE gateway; check Redis Streams if the UI is missing updates.
- `scripts/monitoring/celery_integration.py` hooks the worker into shared observability pipelines. Import failures are suppressed for local development.

---

## Related Documentation

- [Celery Worker Creation Guide](../../../docs/Technical%20Documentation/celery-worker-creation-guide.md)
- [Deal/Meeting Matching Flow](../../../docs/Technical%20Documentation/deal-meeting-matching-flow.md)
- [Email–Deal Mapping](../../../docs/Technical%20Documentation/deal/email-deal-mapping.md)
- Stakeholder intelligence notes (if present) live under `docs/Technical Documentation/`.
