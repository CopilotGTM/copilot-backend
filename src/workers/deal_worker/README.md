# Deal Worker

The **Deal Worker** is a dedicated background worker service responsible for processing asynchronous tasks related to CRM Deals. It focuses on enriching deal data, linking external activities (like meetings), and generating AI-driven insights.

## 📂 Structure

```text
src/workers/deal_worker/
├── services/                 # Core business logic and domain services
│   ├── deal_meeting_matcher.py   # Logic to link Calendar Events to Deals
│   ├── stakeholder_service.py    # Logic to identify and enrich Stakeholders
│   └── briefing_generator.py     # Logic to generate meeting briefings
├── tasks/                    # Celery task definitions
│   └── deal_tasks.py             # Entry points for deal-related tasks
├── celery_app.py             # Celery application instance configuration
└── config.py                 # Worker-specific configuration
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

### 3. Briefing Generator (`services/briefing_generator.py`)

Generates preparation materials for upcoming meetings.

- **Input:** Deal context, past interactions, and stakeholder profiles.
- **Output:** Meeting briefings (objectives, questions to ask, potential objections).

## ⚡ Celery Tasks

Tasks are defined in `tasks/deal_tasks.py` and can be triggered via the Celery broker.

- **`deal.process_deal_meetings_task`**:
  - **Input:** `organisation_id` (UUID string)
  - **Action:** Runs the `DealMeetingMatcher` for all open deals in the specified organisation.
  - **Schedule:** Typically runs nightly or can be triggered manually.

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
celery -A src.workers.deal_worker.celery_app worker -l info -Q deal --pool=solo
```

### Bash (Linux/Mac)

```bash
celery -A src.workers.deal_worker.celery_app worker --loglevel=info -Q deal
```

## 📚 Related Documentation

- [Deal-Meeting Matching Logic Flow](../../../docs/Technical%20Documentation/deal-meeting-matching-flow.md)
