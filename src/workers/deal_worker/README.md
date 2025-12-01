# Deal Worker

The **Deal Worker** is a dedicated Celery background worker responsible for processing asynchronous tasks related to CRM Deals. It focuses on enriching deal data, linking external activities (like meetings), and generating AI-driven insights.

## 📂 Structure

```text
src/workers/deal_worker/
├── __init__.py                   # Package marker
├── celery_app.py                 # Celery application instance + signals
├── config.py                     # Worker-specific configuration
├── README.md                     # This file
├── services/                     # Core business logic and domain services
│   ├── deal_meeting_matcher.py   # Logic to link Calendar Events to Deals
│   ├── stakeholder_service.py    # Logic to identify and enrich Stakeholders
│   ├── deal_job_handlers.py      # Job handlers for briefing, summary, etc.
│   ├── deal_job_publisher.py     # Redis publisher for SSE streaming
│   └── deal_job_utils.py         # Utility functions for job processing
├── tasks/                        # Celery task definitions
│   ├── __init__.py               # Task module exports
│   ├── deal_tasks.py             # Deal matching and processing tasks
│   ├── deal_meeting_tasks.py     # Meeting job tasks (briefing, summary, followup)
│   └── stakeholder_tasks.py      # Stakeholder sync tasks
└── utils/                        # Worker utilities
```

## ⚡ Queues

| Queue | Purpose |
|-------|---------|
| `deal` | Deal matching, processing, and stakeholder sync tasks |
| `deal_jobs` | Meeting job tasks (briefing, summary, followup) |

## 🎯 Tasks

### Deal Tasks (`tasks/deal_tasks.py`)

| Task Name | Description |
|-----------|-------------|
| `deal.process_deal_meetings_task` | Process deal meetings for an organisation |
| `deal.trigger_deal_meeting_processing_all_orgs` | Periodic task to trigger processing for all organisations |
| `deal.process_single_deal_task` | Process meetings for a single deal |

### Deal Meeting Tasks (`tasks/deal_meeting_tasks.py`)

| Task Name | Description |
|-----------|-------------|
| `deal_meeting_task.process` | Generic job processor for meeting-related jobs (briefing, summary, followup) |

### Stakeholder Tasks (`tasks/stakeholder_tasks.py`)

| Task Name | Description |
|-----------|-------------|
| `stakeholder.sync_from_meeting_task` | Sync stakeholders from a calendar event to a deal |

## 🛠️ Key Services

### 1. Deal Meeting Matcher (`services/deal_meeting_matcher.py`)

Automatically identifies and links `CalendarEvent` records to `CRMDeal` records.

- **Logic:** Matches based on the Deal Owner (organiser) and the external Organisation's domain (attendees).
- **Output:** Creates `CalendarEventDealAssociation` records.

### 2. Stakeholder Service (`services/stakeholder_service.py`)

Extracts and manages stakeholders involved in a deal.

- **Discovery:** Identifies new contacts from meeting attendee lists.
- **Enrichment:** Tracks engagement, stance, and influence based on interactions.

### 3. Deal Job Handlers (`services/deal_job_handlers.py`)

Handles different types of deal meeting jobs:

- **Meeting Briefing:** Pre-meeting preparation and context generation.
- **Meeting Summary:** Post-meeting summary from transcripts.
- **Stakeholder Analysis:** Stakeholder insights and recommendations.

## 📡 Triggering Tasks

### From Application Code

```python
from src.workers.deal_worker.celery_app import app as deal_worker_app

# Trigger for a specific organisation
deal_worker_app.send_task(
    "deal.process_deal_meetings_task",
    args=[str(organisation_id)],
    queue="deal",
    routing_key="deal",
)

# Trigger for a single deal
deal_worker_app.send_task(
    "deal.process_single_deal_task",
    args=[str(organisation_id), str(deal_id)],
    queue="deal",
    routing_key="deal",
)

# Trigger stakeholder sync
deal_worker_app.send_task(
    "stakeholder.sync_from_meeting_task",
    args=[str(organisation_id), str(deal_id), str(event_id)],
    queue="deal",
    routing_key="deal",
)
```

### Using Task Delay (Alternative)

```python
from src.workers.deal_worker.tasks.deal_tasks import process_deal_meetings_task

# Trigger for a specific organisation
process_deal_meetings_task.delay(organisation_id="<org_uuid>")
```

## 🚀 Running the Worker

### Development (Windows PowerShell)

```powershell
celery -A src.workers.deal_worker.celery_app worker -l info -Q deal,deal_jobs --pool=solo
```

### Development (Linux/Mac)

```bash
celery -A src.workers.deal_worker.celery_app worker --loglevel=info -Q deal,deal_jobs --pool=solo
```

### With Celery Beat (Periodic Tasks)

```bash
celery -A src.workers.deal_worker.celery_app beat -l info
```

### Production

```bash
celery -A src.workers.deal_worker.celery_app worker -l warning -Q deal,deal_jobs --concurrency=4
```

> **Note:** The worker must listen to both `deal` and `deal_jobs` queues.

## ⏰ Periodic Tasks (Celery Beat)

| Schedule | Task | Description |
|----------|------|-------------|
| Every 3 hours | `deal.trigger_deal_meeting_processing_all_orgs` | Process deal meetings for all active organisations |

## 🔧 Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `REDIS_HOST` | Redis host | `localhost` |
| `REDIS_PORT` | Redis port | `6379` |
| `REDIS_PASSWORD` | Redis password | `None` |
| `MAX_TENANT_ENGINES` | Max tenant DB engines (set in celery_app.py) | `1` |
| `TENANT_POOL_MULTIPLIER` | Tenant pool multiplier | `1.0` |

## 🐛 Debugging

```bash
# Check worker status
celery -A src.workers.deal_worker.celery_app inspect active

# List registered tasks
celery -A src.workers.deal_worker.celery_app inspect registered

# Check queue lengths
celery -A src.workers.deal_worker.celery_app inspect reserved

# Task events in real-time
celery -A src.workers.deal_worker.celery_app events
```

## 📚 Related Documentation

- [Celery Worker Creation Guide](../../../docs/Technical%20Documentation/celery-worker-creation-guide.md)
- [Deal-Meeting Matching Logic Flow](../../../docs/Technical%20Documentation/deal-meeting-matching-flow.md)
