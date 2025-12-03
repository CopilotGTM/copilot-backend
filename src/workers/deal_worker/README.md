# Deal Worker

The **Deal Worker** is a Celery application dedicated to deal-centric background processing: matching meetings to CRM deals, syncing stakeholders, running AI workloads, and streaming results back to the product UI.

---

## Responsibilities at a Glance

- Match `CalendarEvent` records to `CRMDeal` entries and maintain associations.
- Discover, sync, and enrich deal stakeholders using attendee lists and CRM context.
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
|   |-- deal_job_handlers.py   # Meeting job handlers (briefing/summary/follow-up)
|   |-- deal_job_publisher.py  # Redis Stream publisher for SSE updates
|   |-- deal_job_utils.py      # Shared helpers (context builders, fetchers)
|   |-- deal_meeting_matcher.py
|   |-- deal_summary_service.py
|   |-- stakeholder_analysis_service.py
|   `-- stakeholder_service.py
|-- tasks/
|   |-- __init__.py
|   |-- deal_meeting_tasks.py
|   |-- deal_summary_tasks.py
|   |-- deal_tasks.py
|   |-- stakeholder_analysis_tasks.py
|   `-- stakeholder_tasks.py
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
  - `deal_job_handlers.py` + `deal_job_publisher.py` - power the streaming meeting jobs (briefing, summary, follow-up) and publish progress/results via Redis Streams for SSE consumption.
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

### Meeting Job Runner (`tasks/deal_meeting_tasks.py`)

| Task Name | Queue | Description |
|-----------|-------|-------------|
| `deal_meeting_task.process` | `deal_jobs` | Loads a `DealJobs` row, dispatches to `deal_job_handlers`, streams progress/results to Redis/SSE. Handles meeting briefings, meeting summaries, and upcoming follow-up jobs. |

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
| Every 3 hours | `deal.trigger_deal_meeting_processing_all_orgs` | Reconcile meetings <-> deals for every approved organisation. |
| Daily @ 02:00 UTC | `stakeholder.trigger_analysis_all_orgs` | Refresh stakeholder intelligence snapshots from meeting transcripts. |

Schedules live in `DealWorkerConfig.beat_schedule`; adjust there when adding new periodic jobs.

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
- Stakeholder intelligence notes (if present) live under `docs/Technical Documentation/`.
