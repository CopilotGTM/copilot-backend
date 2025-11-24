# Deal-Meeting Matching Logic & Architecture

## 1. Objective

The goal is to automatically identify and link Calendar Meetings to CRM Deals. This allows users to see a complete timeline of interactions associated with a deal. The system must support:

- Automatic discovery based on participants and deal context.
- Manual confirmation/rejection by users.
- Future-proofing for "Technical Owner" assignment.
- Scalable execution (Single Deal, Single Org, All Orgs).

## 2. Core Logic

### 2.1. Owner Identification

We need to determine *who* from our internal team is managing the deal to look up their calendar.

- **Current State:** Use `CRMDeal.owner_email`.
- **Future State:** Use `CRMDeal.technical_owner_email` (or similar field) if available, falling back to `owner_email`.
- **Logic:**

  ```python
  target_email = deal.technical_owner_email or deal.owner_email
  ```

### 2.2. Context Identification (Target Domain)

We need to determine the *external* entity associated with the deal to filter meeting attendees.

- **Primary Source:** `CRMDeal.company_domain`.
- **Secondary Source:** Extract domain from `CRMDeal.primary_contact_email`.
- **Logic:**
  1. If `company_domain` exists, use it.
  2. Else if `primary_contact_email` exists, extract domain (e.g., `john@acme.com` -> `acme.com`).
  3. Exclude generic domains (gmail.com, outlook.com, etc.).

### 2.3. Matching Algorithm

A meeting is a match if:

1. **Organiser/Participant:** The Deal Owner is the organiser OR an attendee.
2. **External Participant:** At least one attendee matches the **Target Domain**.
3. **Timeframe:** Meeting occurs after Deal Creation (optional, but logical) or within a relevant window.

## 3. Data Model & Schema Recommendation

### Question: Do we need a separate table?

**Yes.** We strongly recommend a separate association table `CalendarEventDealAssociation`.

**Reasons:**

1. **Many-to-Many:** A single meeting can be relevant to multiple deals (e.g., a renewal and an upsell discussion).
2. **State Management:** We need to track the state of the link: `SUGGESTED` (by bot), `CONFIRMED` (by user), `REJECTED` (by user).
3. **Separation of Concerns:** `CRMDealTask` is a canonical representation of a task in the CRM. If we overload it with "suggested" meetings, we risk polluting the CRM sync or having to delete records when a user says "No".
4. **Audit:** We can track *who* confirmed the link and *when*.

### Proposed Schema: `calendar_event_deal_associations`

| Field | Type | Description |
| :--- | :--- | :--- |
| `association_id` | UUID | Primary Key |
| `event_id` | UUID | FK to `calendar_events` |
| `deal_id` | UUID | FK to `crm_deals` |
| `organisation_id` | UUID | Partition key |
| `status` | Enum | `SUGGESTED`, `CONFIRMED`, `REJECTED` |
| `match_score` | Float | Confidence score (e.g., 1.0 for domain match, 0.5 for fuzzy) |
| `briefing_status` | Enum | `NOT_STARTED`, `GENERATING`, `COMPLETED`, `FAILED` |
| `briefing_data` | JSONB | Stores the generated briefing content (Objective, Summary, Stakeholders, Pain Points, Questions, Objections, Checklist) |
| `created_at` | DateTime | When the suggestion was made |
| `updated_at` | DateTime | When the status changed |

*Note: Once a link is `CONFIRMED`, we can THEN create/sync a `CRMDealTask` if we want it to appear in the CRM's activity timeline. The `briefing_data` column supports the "Briefing Room" UI requirements.*

*UI Requirement Note: When rendering the Briefing Room, the system must join with the `deal_stakeholders` table to fetch the `job_title` for each participant, as `CalendarEvent` only stores names and emails.*

## 4. Execution Architecture

We need three entry points for the logic.

### 4.1. Single Deal Processing

**Use Case:** A deal is updated, or a user manually triggers a "Refresh" on a deal.

- **Input:** `deal_id`, `organisation_id`
- **Flow:**
  1. Fetch specific Deal.
  2. Run Matching Algorithm against *all* Calendar Events for the Org (filtered by Owner/Time).
  3. Upsert `CalendarEventDealAssociation` records.

### 4.2. Single Organisation Processing

**Use Case:** Onboarding a new organisation or manual full sync.

- **Input:** `organisation_id`
- **Flow:**
  1. Fetch all OPEN Deals for Org.
  2. For each deal, run **Single Deal Processing**.
  3. *Optimization:* Fetch all relevant Calendar Events for the Org *once* into memory or use a join query to process in bulk.

### 4.3. All Organisations (Scheduled)

**Use Case:** Nightly/Daily batch job.

- **Input:** None (Trigger)
- **Flow:**
  1. Fetch all Active Organisations.
  2. Enqueue a Celery task for **Single Organisation Processing** for each Org.

## 5. Implementation Plan

### Step 1: Database Migration

- Create `calendar_event_deal_associations` table.
- Update `CalendarEvent` and `CRMDeal` models to include relationships (optional but helpful).

### Step 2: Refactor `deal_meeting_matcher.py`

- **Class-based approach:** Create a `DealMeetingMatcher` service class.
- **Methods:**
  - `process_deal(deal_id)`
  - `process_organisation(org_id)`
  - `_find_matches(deal, events)`
- **Logic Update:**
  - Implement the "Technical Owner" fallback logic.
  - Implement the Domain extraction logic.
  - Instead of creating `CRMDealTask` directly, create `CalendarEventDealAssociation` with status `SUGGESTED`.

### Step 3: Update Celery Tasks

- Update `process_deal_meetings_task` to use the new Service.
- Add `process_single_deal_task` for granular execution.

### Step 4: API Layer (Future)

- Endpoints for UI to:
  - List Suggested Meetings for a Deal.
  - Confirm/Reject a Meeting (Update `status` in association table).

## 6. Code Changes Required

### `src/domain/models/organisation_tables/sync/calendar.py`

- Add `CalendarEventDealAssociation` model.

### `src/workers/deal_worker/services/deal_meeting_matcher.py`

- Rewrite `process_deal_meetings` to support granular execution.
- Implement `match_deal_to_meetings` function.

### `src/workers/deal_worker/tasks/deal_tasks.py`

- Add `process_single_deal_task`.
