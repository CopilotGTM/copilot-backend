# Deal Worker

The **Deal Worker** is a dedicated background worker service responsible for processing asynchronous tasks related to CRM Deals. It focuses on enriching deal data, linking external activities (like meetings), and generating AI-driven insights.

## 📂 Structure

```text
src/workers/deal_worker/
├── services/                     # Core business logic and domain services
│   ├── deal_meeting_matcher.py   # Logic to link Calendar Events to Deals
│   ├── stakeholder_service.py    # Logic to identify and enrich Stakeholders
│   ├── deal_job_handlers.py      # Job handlers for briefing, summary, etc.
│   ├── deal_job_publisher.py     # Redis publisher for SSE streaming
│   └── deal_job_utils.py         # Utility functions for job processing
├── tasks/                        # Celery task definitions
│   ├── deal_tasks.py             # Deal matching and processing tasks
│   ├── deal_meeting_tasks.py     # Meeting job tasks (briefing, summary, followup)
│   └── stakeholder_tasks.py      # Stakeholder sync tasks
├── celery_app.py                 # Celery application instance configuration
└── config.py                     # Worker-specific configuration
```

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

## ⚡ Celery Tasks

### Deal Tasks (`tasks/deal_tasks.py`)

- **`deal.process_deal_meetings_task`**: Runs the `DealMeetingMatcher` for all open deals in an organisation.
- **`deal.process_single_deal_task`**: Processes meetings for a single deal.
- **`deal.trigger_deal_meeting_processing_all_orgs`**: Periodic task to trigger processing for all organisations.

### Deal Meeting Tasks (`tasks/deal_meeting_tasks.py`)

- **`deal_meeting_task.process`**: Generic job processor for meeting-related jobs.
  - **Input:** `organisation_id`, `job_id`
  - **Action:** Dispatches to appropriate handler based on job type (briefing, summary, followup).
  - **Queue:** `deal_jobs`

## 📡 Triggering Tasks

You can trigger the deal processing task asynchronously from other parts of the application:

```python
from src.workers.deal_worker.tasks.deal_tasks import process_deal_meetings_task

# Trigger for a specific organisation
process_deal_meetings_task.delay(organisation_id="<org_uuid>")
```

### Payload Object

```json
{
  "organisation_id": "123e4567-e89b-12d3-a456-426614174000"
}
```

## 🚀 Running the Worker

To start the Celery worker for this module:

### PowerShell (Windows)

```powershell
celery -A src.workers.deal_worker.celery_app worker -l info -Q deal,deal_jobs --pool=solo
```

### Bash (Linux/Mac)

```bash
celery -A src.workers.deal_worker.celery_app worker --loglevel=info -Q deal,deal_jobs
```

> **Note:** The worker must listen to both `deal` and `deal_jobs` queues:
>
> - `deal`: For deal matching and processing tasks
> - `deal_jobs`: For meeting job tasks (briefing, summary, followup)

## 📚 Related Documentation

- [Deal-Meeting Matching Logic Flow](../../../docs/Technical%20Documentation/deal-meeting-matching-flow.md)
