# Deprecated Deal Timeline API

This API returns the unified, denormalized timeline for a deal. The timeline includes communications (emails, meetings), CRM lifecycle events (deal created, stage changes), and insights events (risks, blockers, stakeholders). Both list and detail endpoints return the same `TimelineEvent` shape—the detail endpoint simply returns enriched `event_data`.

**Base URL**: `/users/crm/deals`

---

## Response Envelope

All endpoints return the shared `GenericResponse` envelope used across CRM APIs.

```json
{
  "status": "SUCCESS | ERROR",
  "message": "Human-readable message",
  "data": { ... } | null
}
```

---

## Authentication

- Bearer token required (`Authorization: Bearer <access_token>`)
- Organisation/user/role are inferred from the token; clients do not pass these explicitly.

---

## Rate Limiting (defaults)

- Read endpoints (`GET`): ~60/min, 1000/hour per user
- On limit breach: `429 Too Many Requests` with `Retry-After`

---

## Data Shapes (TypeScript)

```ts
export type UUID = string;

type TimelineEventType =
  // Communications
  | "EMAIL"
  | "MEETING"
  // CRM Lifecycle
  | "STAGE_CHANGE"
  | "DEAL_CREATED"
  // Risk/Blocker Events
  | "BLOCKER_ADDED"
  | "BLOCKER_RESOLVED"
  | "BLOCKER_DISMISSED"
  | "RISK_FLAGGED"
  // Stakeholder Events
  | "STAKEHOLDER_ADDED"
  | "STAKEHOLDER_REMOVED";

type TimelineEventSource =
  | "EMAIL_GMAIL"
  | "EMAIL_OUTLOOK"
  | "CALENDAR_GOOGLE"
  | "CALENDAR_OUTLOOK"
  | "CRM_HUBSPOT"
  | "CRM_SALESFORCE"
  | "SYSTEM";  // For system-generated events (risks, blockers, stakeholders)

// ============================================================================
// Unified Timeline Event (same shape for list and detail)
// ============================================================================

export interface TimelineEvent {
  id: UUID;                            // timeline_event_id
  deal_id: UUID;
  event_type: TimelineEventType;
  event_source: TimelineEventSource;
  event_timestamp: string;             // ISO8601
  event_data:
    | EmailEventData
    | MeetingEventData
    | StageChangeEventData
    | DealCreatedEventData
    | BlockerAddedEventData
    | BlockerResolvedEventData
    | BlockerDismissedEventData
    | RiskFlaggedEventData
    | StakeholderAddedEventData
    | StakeholderRemovedEventData;
  excluded_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

// ============================================================================
// Event Data Types (shape varies by event_type)
// ============================================================================

// EMAIL event_data
export interface EmailEventData {
  // Source ID for API calls (e.g., fetching full email details)
  email_id?: string | null;           // Internal UUID of the email record

  // Provider icon/logo URL (derived from event_source)
  provider_logo_url?: string | null;

  // --- List Preview (always present) ---
  subject: string | null;
  snippet: string | null;
  from_email: string | null;

  // --- Detail Enrichment (populated on detail fetch) ---
  from_name?: string | null;
  to_emails?: string[] | null;
  cc_emails?: string[] | null;
  thread_id?: string | null;

  // Body content (detail only)
  body_text?: string | null;           // Plain text body
  body_html?: string | null;           // HTML body
  body_truncated?: boolean | null;     // True if body was truncated (>50K chars)
  body_length?: number | null;         // Original body length in characters

  // Attachments (detail only)
  has_attachments?: boolean | null;
  attachments?: EmailAttachment[] | null;

  // Timing (detail only)
  received_at?: string | null;         // ISO8601 - when email was received
  sent_at?: string | null;             // ISO8601 - when email was sent
  direction?: "INBOUND" | "OUTBOUND" | null;
}

export interface EmailAttachment {
  name: string | null;
  size: number | null;                 // Size in bytes
  mime_type: string | null;
}

// MEETING event_data
export interface MeetingEventData {
  // Source ID for API calls (e.g., summary generation, prep generation)
  calendar_event_id?: string | null;  // Internal UUID of the calendar event

  // Provider icon/logo URL (derived from event_source)
  provider_logo_url?: string | null;

  // --- List Preview (always present) ---
  title: string | null;
  duration_minutes: number | null;
  attendee_count: number | null;

  // --- Detail Enrichment (populated on detail fetch) ---
  attendees?: string[] | null;         // Full list of attendee emails
  meeting_summary?: MeetingSummary | null;
}

export interface MeetingSummary {
  status: "PENDING" | "PROCESSING" | "COMPLETED" | "FAILED" | null;
  data: Record<string, unknown> | null;
}

// STAGE_CHANGE event_data
export interface StageChangeEventData {
  // Provider icon/logo URL (derived from event_source)
  provider_logo_url?: string | null;

  // Stage transition
  old_stage: string | null;              // Previous stage ID/name
  new_stage: string | null;              // New stage ID/name
  old_stage_label?: string | null;       // Display label for old stage
  new_stage_label?: string | null;       // Display label for new stage

  // Deal context (included for frontend convenience - no separate API call needed)
  deal_name?: string | null;
  company_name?: string | null;
  amount?: number | null;
  currency?: string | null;
  pipeline_name?: string | null;
}

// DEAL_CREATED event_data
export interface DealCreatedEventData {
  // Provider icon/logo URL (derived from event_source)
  provider_logo_url?: string | null;

  // Deal information
  deal_name: string | null;
  initial_stage?: string | null;
  pipeline_name?: string | null;
  amount?: number | null;
  currency?: string | null;

  // Company information (included for frontend convenience)
  company_name?: string | null;
  company_domain?: string | null;

  // Owner information
  owner_name?: string | null;

  // Primary contact
  primary_contact_name?: string | null;
  primary_contact_email?: string | null;
}

// ============================================================================
// Risk/Blocker Event Data Types
//
// These match the RiskBlockerListItem fields shown in the Risks & Blockers panel.
// For full details (audit trail, provenance), use: GET /deals/risks-blockers/{risk_blocker_id}
// ============================================================================

type RiskBlockerSeverity = "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";
type RiskBlockerStatus = "OPEN" | "IN_PROGRESS" | "RESOLVED" | "DISMISSED" | "WONT_FIX";
type RiskBlockerCategory =
  | "TECHNICAL"
  | "INTEGRATION"
  | "SECURITY"
  | "PERFORMANCE"
  | "BUDGET"
  | "TIMELINE"
  | "CONTRACT"
  | "CHAMPION_RISK"
  | "STAKEHOLDER"
  | "COMPETITION"
  | "INTERNAL_PROCESS"
  | "RESOURCE"
  | "OTHER";
type RiskBlockerSource =
  | "EMAIL_GMAIL" | "EMAIL_OUTLOOK"
  | "MEETING_GOOGLE" | "MEETING_OUTLOOK" | "TEAMS_MEETING" | "ZOOM_MEETING"
  | "SLACK" | "TEAMS_CHAT"
  | "CRM_HUBSPOT" | "CRM_SALESFORCE"
  | "MANUAL" | "SYSTEM";

// BLOCKER_ADDED / RISK_FLAGGED detail event_data (smart data from record)
export interface RiskBlockerDetailEventData {
  // ID for navigation to full detail view
  risk_blocker_id: string;

  // Classification (aligned with RiskBlockerListItem)
  item_type: "RISK" | "BLOCKER";
  severity: RiskBlockerSeverity;
  category: RiskBlockerCategory;
  status: RiskBlockerStatus;

  // Content (title + quote for preview, description for context)
  title: string;
  description: string | null;
  quote: string | null;              // Direct quote from source (for AI-extracted)

  // Source (for icon display)
  source_type: RiskBlockerSource;
  source_context: string | null;     // e.g., "Discovery Call - Dec 15"

  // AI indicator (for badge)
  is_ai_generated: boolean;

  // Owner (for assignment display)
  owner_name: string | null;

  // Resolution summary (only for resolved items)
  resolution_notes: string | null;
}

// List view (preview) event_data - minimal fields
export interface BlockerAddedEventData {
  provider_logo_url?: string | null;
  risk_blocker_id: string | null;
  item_type: "BLOCKER";
  severity: RiskBlockerSeverity | null;
  category: RiskBlockerCategory | null;
  is_ai_generated: boolean | null;
}

export interface BlockerResolvedEventData {
  provider_logo_url?: string | null;
  risk_blocker_id: string | null;
  item_type: "BLOCKER";
  severity: RiskBlockerSeverity | null;
  resolution_notes: string | null;
}

export interface BlockerDismissedEventData {
  provider_logo_url?: string | null;
  risk_blocker_id: string | null;
  item_type: "BLOCKER";
  severity: RiskBlockerSeverity | null;
}

export interface RiskFlaggedEventData {
  provider_logo_url?: string | null;
  risk_blocker_id: string | null;
  severity: RiskBlockerSeverity | null;
  category: RiskBlockerCategory | null;
  is_ai_generated: boolean | null;
}

// ============================================================================
// Stakeholder Event Data Types
//
// These match the StakeholderDetail fields shown in the Stakeholders panel.
// For full details (engagement history), use: GET /deals/{deal_id}/stakeholders/{stakeholder_id}
// ============================================================================

type StakeholderSource = "CRM" | "CALENDAR_DISCOVERY" | "EMAIL_DISCOVERY" | "MANUAL";
type StakeholderStance = "UNKNOWN" | "CHAMPION" | "SUPPORTER" | "NEUTRAL" | "DETRACTOR" | "BLOCKER";
type DecisionPower = "UNKNOWN" | "DECISION_MAKER" | "INFLUENCER" | "EVALUATOR" | "GATEKEEPER" | "END_USER";
type StakeholderTemperature = "UNKNOWN" | "HOT" | "WARM" | "COLD";

// STAKEHOLDER_ADDED detail event_data (smart data from record)
export interface StakeholderDetailEventData {
  // ID for navigation to full detail view
  stakeholder_id: string;

  // Identity (core fields)
  email: string;
  name: string | null;
  job_title: string | null;

  // Key insights (aligned with StakeholderDetail)
  source: StakeholderSource;
  stance: StakeholderStance;
  decision_power: DecisionPower;

  // Engagement indicators
  temperature: StakeholderTemperature;
  last_contact_at: string | null;    // ISO8601
}

// List view (preview) event_data - minimal fields
export interface StakeholderAddedEventData {
  provider_logo_url?: string | null;
  stakeholder_id: string | null;
  email: string | null;
  name: string | null;
  job_title: string | null;
  source: StakeholderSource | null;
  stance: StakeholderStance | null;
}

export interface StakeholderRemovedEventData {
  provider_logo_url?: string | null;
  stakeholder_id: string | null;
  email: string | null;
  name: string | null;
}

// ============================================================================
// Pagination
// ============================================================================

export interface TimelinePage {
  deal_id: UUID;
  items: TimelineEvent[];
  total_count: number;
  cursor: string | null;               // opaque pagination cursor
}

export interface ActivityFeedItem {
  activity_id: UUID;
  activity_type: string;
  source_type: string;
  source_record_id: UUID | null;
  source_provider: string | null;
  source_integration_id: UUID | null;
  source_url: string | null;
  title: string | null;
  summary: string | null;
  preview: string | null;
  occurred_at: string;
  actor_name: string | null;
  actor_email: string | null;
  display_payload: Record<string, unknown>;
  link_role: string | null;
  link_source: string | null;
  confidence_score: number | null;
  created_at: string;
  updated_at: string;
}

export interface ActivityFeedPage {
  object_type: "DEAL";
  object_id: UUID;
  items: ActivityFeedItem[];
  total_count: number;
  cursor: string | null;
}
```

---

## 1) List Deal Timeline

- **Endpoint**: `GET /{deal_id}/timeline`
- **Purpose**: Retrieve a paginated, chronological timeline for a deal.
- **Tags**: `timeline`

### Query Parameters

| Name | Type | Description |
| ---- | ---- | ----------- |
| `limit` | int (1-100, default 20) | Page size |
| `cursor` | string | Opaque pagination cursor from previous page |
| `event_types` | string | Comma-separated filter (see supported types below) |
| `start_date` | datetime | Include events after this timestamp |
| `end_date` | datetime | Include events before this timestamp |
| `include_excluded` | boolean (default false) | When true, include soft-hidden events |
| `sort` | `asc` \| `desc` (default `desc`) | Sort order |

### Supported Event Types for Filtering

```
EMAIL, MEETING, STAGE_CHANGE, DEAL_CREATED,
BLOCKER_ADDED, BLOCKER_RESOLVED, BLOCKER_DISMISSED, RISK_FLAGGED,
STAKEHOLDER_ADDED, STAKEHOLDER_REMOVED
```

Example: `event_types=EMAIL,MEETING,BLOCKER_ADDED,RISK_FLAGGED`

### Sample Request

```http
GET /6c3f98d5-2e6b-4a1c-8f8c-2e8f0211b0b9/timeline?limit=20&sort=desc
Authorization: Bearer <token>
```

### Sample Response

```json
{
  "status": "SUCCESS",
  "message": "Timeline retrieved successfully",
  "data": {
    "deal_id": "6c3f98d5-2e6b-4a1c-8f8c-2e8f0211b0b9",
    "items": [
      {
        "id": "f9a6c1e1-0d56-4e2b-9c2d-4d3fb3d6c9a1",
        "deal_id": "6c3f98d5-2e6b-4a1c-8f8c-2e8f0211b0b9",
        "event_type": "MEETING",
        "event_source": "CALENDAR_GOOGLE",
        "event_timestamp": "2025-01-15T10:00:00Z",
        "event_data": {
          "calendar_event_id": "d5e6f7a8-1234-5678-9abc-def012345678",
          "provider_logo_url": "https://api.nango.dev/images/template-logos/google-calendar.svg",
          "title": "Q1 Planning Call with Acme Corp",
          "duration_minutes": 60,
          "attendee_count": 4
        },
        "excluded_at": null,
        "created_at": "2025-01-10T08:00:00Z",
        "updated_at": "2025-01-15T11:00:00Z"
      },
      {
        "id": "a1b2c3d4-5678-9abc-def0-123456789abc",
        "deal_id": "6c3f98d5-2e6b-4a1c-8f8c-2e8f0211b0b9",
        "event_type": "EMAIL",
        "event_source": "EMAIL_GMAIL",
        "event_timestamp": "2025-01-14T15:30:00Z",
        "event_data": {
          "email_id": "e1f2a3b4-5678-9abc-def0-abcdef123456",
          "provider_logo_url": "https://app.nango.dev/images/template-logos/google-mail.svg",
          "subject": "Re: Proposal Review",
          "snippet": "Thanks for sending over the proposal. I've reviewed it with...",
          "from_email": "jane@acme.com"
        },
        "excluded_at": null,
        "created_at": "2025-01-14T15:30:00Z",
        "updated_at": "2025-01-14T15:30:00Z"
      }
    ],
    "total_count": 45,
    "cursor": "eyJvZmZzZXQiOjIw..."
  }
}
```

---

## 2) List Deal Activity Feed

- **Endpoint**: `GET /{deal_id}/activity-feed`
- **Purpose**: Retrieve the CRM activity projection linked to a deal.
- **Tags**: `timeline`

This endpoint is mounted on the existing deal timeline router. It reads
`crm_activity_links -> crm_activities`, not the legacy `DealTimelineEvent`
table used by `GET /{deal_id}/timeline`.

### Query Parameters

| Name | Type | Description |
| ---- | ---- | ----------- |
| `limit` | int (1-100, default 20) | Page size |
| `cursor` | string | Opaque pagination cursor from previous page |
| `activity_types` | string | Comma-separated activity type filter |
| `source_types` | string | Comma-separated source type filter |
| `start_date` | datetime | Include linked activities after this timestamp |
| `end_date` | datetime | Include linked activities before this timestamp |
| `sort` | `asc` \| `desc` (default `desc`) | Sort order |

### Sample Request

```http
GET /6c3f98d5-2e6b-4a1c-8f8c-2e8f0211b0b9/activity-feed?limit=20&sort=desc
Authorization: Bearer <token>
```

### Sample Response

```json
{
  "status": "SUCCESS",
  "message": "Activity feed retrieved successfully",
  "data": {
    "object_type": "DEAL",
    "object_id": "6c3f98d5-2e6b-4a1c-8f8c-2e8f0211b0b9",
    "items": [
      {
        "activity_id": "f9a6c1e1-0d56-4e2b-9c2d-4d3fb3d6c9a1",
        "activity_type": "NOTE",
        "source_type": "CRM_NOTE",
        "source_record_id": "d5e6f7a8-1234-5678-9abc-def012345678",
        "source_provider": "hubspot",
        "source_integration_id": "c7d8e9f0-a1b2-3456-cdef-789abcdef012",
        "source_url": null,
        "title": "Discovery notes",
        "summary": null,
        "preview": "Customer asked for security review next week.",
        "occurred_at": "2026-06-20T10:00:00Z",
        "actor_name": "Jane Smith",
        "actor_email": "jane@example.com",
        "display_payload": {},
        "link_role": "PRIMARY",
        "link_source": "CRM_ASSOCIATION",
        "confidence_score": null,
        "created_at": "2026-06-20T10:05:00Z",
        "updated_at": "2026-06-20T10:05:00Z"
      }
    ],
    "total_count": 1,
    "cursor": null
  }
}
```

---

## 3) Get Timeline Event Detail

- **Endpoint**: `GET /timeline/{timeline_event_id}`
- **Purpose**: Fetch the same `TimelineEvent` shape with enriched `event_data`.
- **Tags**: `timeline`

### Path Parameters

| Name | Type | Description |
| ---- | ---- | ----------- |
| `timeline_event_id` | UUID | Timeline event identifier |

### Sample Response (Meeting)

```json
{
  "status": "SUCCESS",
  "message": "Timeline event retrieved successfully",
  "data": {
    "id": "f9a6c1e1-0d56-4e2b-9c2d-4d3fb3d6c9a1",
    "deal_id": "6c3f98d5-2e6b-4a1c-8f8c-2e8f0211b0b9",
    "event_type": "MEETING",
    "event_source": "CALENDAR_GOOGLE",
    "event_timestamp": "2025-01-15T10:00:00Z",
    "event_data": {
      "calendar_event_id": "d5e6f7a8-1234-5678-9abc-def012345678",
      "provider_logo_url": "https://api.nango.dev/images/template-logos/google-calendar.svg",
      "title": "Q1 Planning Call with Acme Corp",
      "duration_minutes": 60,
      "attendee_count": 4,
      "attendees": ["john@company.com", "jane@acme.com", "bob@acme.com", "alice@acme.com"],
      "meeting_summary": {
        "status": "COMPLETED",
        "data": {
          "key_points": ["Budget approved", "Timeline confirmed"],
          "action_items": [
            { "task": "Send revised proposal", "owner": "John", "due": "2025-01-20" }
          ]
        }
      }
    },
    "excluded_at": null,
    "created_at": "2025-01-10T08:00:00Z",
    "updated_at": "2025-01-15T11:00:00Z"
  }
}
```

### Sample Response (Email)

```json
{
  "status": "SUCCESS",
  "message": "Timeline event retrieved successfully",
  "data": {
    "id": "a1b2c3d4-5678-9abc-def0-123456789abc",
    "deal_id": "6c3f98d5-2e6b-4a1c-8f8c-2e8f0211b0b9",
    "event_type": "EMAIL",
    "event_source": "EMAIL_GMAIL",
    "event_timestamp": "2025-01-14T15:30:00Z",
    "event_data": {
      "email_id": "e1f2a3b4-5678-9abc-def0-abcdef123456",
      "provider_logo_url": "https://app.nango.dev/images/template-logos/google-mail.svg",
      "subject": "Re: Proposal Review",
      "snippet": "Thanks for sending over the proposal. I've reviewed it with the team...",
      "from_email": "jane@acme.com",
      "from_name": "Jane Smith",
      "to_emails": ["john@company.com"],
      "cc_emails": ["sales@company.com"],
      "thread_id": "AAQkAbc123...",
      "body_text": "Thanks for sending over the proposal. I've reviewed it with the team and we have a few questions...",
      "body_html": "<div>Thanks for sending over the proposal. I've reviewed it with the team...</div>",
      "body_truncated": false,
      "body_length": 1524,
      "has_attachments": true,
      "attachments": [
        {
          "name": "Proposal_v2.pdf",
          "size": 245760,
          "mime_type": "application/pdf"
        }
      ],
      "received_at": "2025-01-14T15:30:00Z",
      "sent_at": "2025-01-14T15:29:45Z",
      "direction": "INBOUND"
    },
    "excluded_at": null,
    "created_at": "2025-01-14T15:30:00Z",
    "updated_at": "2025-01-14T15:30:00Z"
  }
}
```

### Sample Response (Stage Change)

```json
{
  "status": "SUCCESS",
  "message": "Timeline event retrieved successfully",
  "data": {
    "id": "b2c3d4e5-6789-abcd-ef01-234567890abc",
    "deal_id": "6c3f98d5-2e6b-4a1c-8f8c-2e8f0211b0b9",
    "event_type": "STAGE_CHANGE",
    "event_source": "CRM_HUBSPOT",
    "event_timestamp": "2025-01-13T09:15:00Z",
    "event_data": {
      "provider_logo_url": "https://app.nango.dev/images/template-logos/hubspot.svg",
      "old_stage": "discovery",
      "new_stage": "proposal",
      "old_stage_label": "Discovery",
      "new_stage_label": "Proposal Sent",
      "deal_name": "Acme Corp Enterprise License",
      "company_name": "Acme Corporation",
      "amount": 75000.00,
      "currency": "USD",
      "pipeline_name": "Sales Pipeline"
    },
    "excluded_at": null,
    "created_at": "2025-01-13T09:15:00Z",
    "updated_at": "2025-01-13T09:15:00Z"
  }
}
```

### Sample Response (Deal Created)

```json
{
  "status": "SUCCESS",
  "message": "Timeline event retrieved successfully",
  "data": {
    "id": "c3d4e5f6-7890-bcde-f012-345678901bcd",
    "deal_id": "6c3f98d5-2e6b-4a1c-8f8c-2e8f0211b0b9",
    "event_type": "DEAL_CREATED",
    "event_source": "CRM_HUBSPOT",
    "event_timestamp": "2025-01-10T08:00:00Z",
    "event_data": {
      "provider_logo_url": "https://app.nango.dev/images/template-logos/hubspot.svg",
      "deal_name": "Acme Corp Enterprise License",
      "initial_stage": "Discovery",
      "pipeline_name": "Sales Pipeline",
      "amount": 75000.00,
      "currency": "USD",
      "company_name": "Acme Corporation",
      "company_domain": "acme.com",
      "owner_name": "John Smith",
      "primary_contact_name": "Jane Doe",
      "primary_contact_email": "jane.doe@acme.com"
    },
    "excluded_at": null,
    "created_at": "2025-01-10T08:00:00Z",
    "updated_at": "2025-01-10T08:00:00Z"
  }
}
```

### Sample Response (Blocker Added - Detail View)

**Note:** Detail view returns enriched "smart data" matching the Risks & Blockers panel.
For full details (audit trail, provenance), use: `GET /deals/risks-blockers/{risk_blocker_id}`

```json
{
  "status": "SUCCESS",
  "message": "Timeline event retrieved successfully",
  "data": {
    "id": "d4e5f6a7-8901-cdef-0123-456789012cde",
    "deal_id": "6c3f98d5-2e6b-4a1c-8f8c-2e8f0211b0b9",
    "event_type": "BLOCKER_ADDED",
    "event_source": "SYSTEM",
    "event_timestamp": "2025-01-14T11:30:00Z",
    "event_data": {
      "risk_blocker_id": "a1b2c3d4-5678-9abc-def0-111111111111",
      "item_type": "BLOCKER",
      "severity": "HIGH",
      "category": "BUDGET",
      "status": "OPEN",
      "title": "Budget not approved for Q1",
      "description": "Customer's finance team has not approved the budget allocation for this project. They are waiting for Q1 planning to finalize.",
      "quote": "We're still waiting on budget approval from finance before we can proceed",
      "source_type": "MEETING_GOOGLE",
      "source_context": "Discovery Call - Jan 14",
      "is_ai_generated": true,
      "owner_name": "John Smith",
      "resolution_notes": null
    },
    "excluded_at": null,
    "created_at": "2025-01-14T11:30:00Z",
    "updated_at": "2025-01-14T11:30:00Z"
  }
}
```

### Sample Response (Blocker Resolved)

```json
{
  "status": "SUCCESS",
  "message": "Timeline event retrieved successfully",
  "data": {
    "id": "e5f6a7b8-9012-def0-1234-567890123def",
    "deal_id": "6c3f98d5-2e6b-4a1c-8f8c-2e8f0211b0b9",
    "event_type": "BLOCKER_RESOLVED",
    "event_source": "SYSTEM",
    "event_timestamp": "2025-01-16T14:00:00Z",
    "event_data": {
      "provider_logo_url": null,
      "risk_blocker_id": "a1b2c3d4-5678-9abc-def0-111111111111",
      "item_type": "BLOCKER",
      "severity": "HIGH",
      "resolution_notes": "Budget approved by CFO after presenting ROI analysis"
    },
    "excluded_at": null,
    "created_at": "2025-01-16T14:00:00Z",
    "updated_at": "2025-01-16T14:00:00Z"
  }
}
```

### Sample Response (Blocker Dismissed)

```json
{
  "status": "SUCCESS",
  "message": "Timeline event retrieved successfully",
  "data": {
    "id": "f6a7b8c9-0123-ef01-2345-678901234ef0",
    "deal_id": "6c3f98d5-2e6b-4a1c-8f8c-2e8f0211b0b9",
    "event_type": "BLOCKER_DISMISSED",
    "event_source": "SYSTEM",
    "event_timestamp": "2025-01-15T09:00:00Z",
    "event_data": {
      "provider_logo_url": null,
      "risk_blocker_id": "b2c3d4e5-6789-abcd-ef01-222222222222",
      "item_type": "BLOCKER",
      "severity": "LOW"
    },
    "excluded_at": null,
    "created_at": "2025-01-15T09:00:00Z",
    "updated_at": "2025-01-15T09:00:00Z"
  }
}
```

### Sample Response (Risk Flagged)

```json
{
  "status": "SUCCESS",
  "message": "Timeline event retrieved successfully",
  "data": {
    "id": "a7b8c9d0-1234-f012-3456-789012345f01",
    "deal_id": "6c3f98d5-2e6b-4a1c-8f8c-2e8f0211b0b9",
    "event_type": "RISK_FLAGGED",
    "event_source": "SYSTEM",
    "event_timestamp": "2025-01-14T11:30:00Z",
    "event_data": {
      "provider_logo_url": null,
      "risk_blocker_id": "c3d4e5f6-7890-bcde-f012-333333333333",
      "severity": "MEDIUM",
      "category": "CHAMPION_RISK",
      "is_ai_generated": true
    },
    "excluded_at": null,
    "created_at": "2025-01-14T11:30:00Z",
    "updated_at": "2025-01-14T11:30:00Z"
  }
}
```

### Sample Response (Stakeholder Added - Detail View)

**Note:** Detail view returns enriched "smart data" matching the Stakeholders panel.
For full details (engagement history), use: `GET /deals/{deal_id}/stakeholders/{stakeholder_id}`

```json
{
  "status": "SUCCESS",
  "message": "Timeline event retrieved successfully",
  "data": {
    "id": "b8c9d0e1-2345-0123-4567-890123456012",
    "deal_id": "6c3f98d5-2e6b-4a1c-8f8c-2e8f0211b0b9",
    "event_type": "STAKEHOLDER_ADDED",
    "event_source": "SYSTEM",
    "event_timestamp": "2025-01-12T10:00:00Z",
    "event_data": {
      "stakeholder_id": "d4e5f6a7-8901-cdef-0123-444444444444",
      "email": "cfo@acme.com",
      "name": "Robert Johnson",
      "job_title": "Chief Financial Officer",
      "source": "CALENDAR_DISCOVERY",
      "stance": "NEUTRAL",
      "decision_power": "DECISION_MAKER",
      "temperature": "WARM",
      "last_contact_at": "2025-01-15T10:00:00Z"
    },
    "excluded_at": null,
    "created_at": "2025-01-12T10:00:00Z",
    "updated_at": "2025-01-12T10:00:00Z"
  }
}
```

### Sample Response (Stakeholder Removed)

```json
{
  "status": "SUCCESS",
  "message": "Timeline event retrieved successfully",
  "data": {
    "id": "c9d0e1f2-3456-1234-5678-901234567123",
    "deal_id": "6c3f98d5-2e6b-4a1c-8f8c-2e8f0211b0b9",
    "event_type": "STAKEHOLDER_REMOVED",
    "event_source": "SYSTEM",
    "event_timestamp": "2025-01-18T16:30:00Z",
    "event_data": {
      "provider_logo_url": null,
      "stakeholder_id": "e5f6a7b8-9012-def0-1234-555555555555",
      "email": "former.employee@acme.com",
      "name": "Alice Williams"
    },
    "excluded_at": null,
    "created_at": "2025-01-18T16:30:00Z",
    "updated_at": "2025-01-18T16:30:00Z"
  }
}
```

---

## Error Codes (common)

| HTTP | When |
| ---- | ---- |
| `400` | Invalid query params or malformed UUID |
| `401` | Missing/invalid bearer token |
| `403` | Authenticated but not authorized for the org/deal |
| `404` | Deal or timeline event not found |
| `429` | Rate limit exceeded |
| `500` | Unexpected server error |

---

## Integration Notes for Frontend

### Unified Model

Both list and detail endpoints return the same `TimelineEvent` interface. The difference:

- **List**: `event_data` contains preview fields only
  - Email: `subject`, `snippet`, `from_email`
  - Meeting: `title`, `duration_minutes`, `attendee_count`
  - Stage Change: All fields (static from metadata)
  - Deal Created: All fields (static from metadata)
  - Risk/Blocker events: Basic fields (`severity`, `category`, `is_ai_generated`)
  - Stakeholder events: Basic fields (`email`, `name`, `job_title`, `source`, `stance`)

- **Detail**: Same preview fields **plus** enriched fields fetched from source records
  - Email: adds `from_name`, `to_emails`, `cc_emails`, `thread_id`, `body_text`, `body_html`, `body_truncated`, `body_length`, `has_attachments`, `attachments`, `received_at`, `sent_at`, `direction`
  - Meeting: adds `attendees[]`, `meeting_summary`
  - **Risk/Blocker events**: Fetches from `deal_risks_blockers` table - adds `status`, `title`, `description`, `quote`, `source_type`, `source_context`, `owner_name`, `resolution_notes`
  - **Stakeholder events**: Fetches stakeholder read data - adds `decision_power`, `temperature`, `last_contact_at`
  - Stage Change / Deal Created: Same as list (all data already in metadata)

### Event Categories

Events fall into three categories for UI grouping:

1. **Communications**: `EMAIL`, `MEETING`
   - Have provider logos (Gmail, Google Calendar, Outlook, etc.)
   - Show communication details

2. **CRM Lifecycle**: `STAGE_CHANGE`, `DEAL_CREATED`
   - Have CRM provider logos (HubSpot, Salesforce)
   - Show deal progression

3. **Insights**: `BLOCKER_ADDED`, `BLOCKER_RESOLVED`, `BLOCKER_DISMISSED`, `RISK_FLAGGED`, `STAKEHOLDER_ADDED`, `STAKEHOLDER_REMOVED`
   - Have `event_source: "SYSTEM"` and `provider_logo_url: null`
   - Use custom icons based on `event_type` (e.g., warning icon for risks, person icon for stakeholders)

### Reusing Existing Detail Views

The timeline detail API returns **smart data** that matches the existing detail panels:

| Event Type | Fields Match | Existing Panel | Full Detail API |
|------------|--------------|----------------|-----------------|
| `BLOCKER_*` / `RISK_FLAGGED` | `RiskBlockerListItem` | Risks & Blockers panel | `GET /deals/risks-blockers/{risk_blocker_id}` |
| `STAKEHOLDER_*` | `StakeholderDetail` | Stakeholders panel | `GET /deals/{deal_id}/stakeholders/{stakeholder_id}` |

**Recommended Frontend Pattern:**

1. **Timeline click → Show inline preview** using the enriched `event_data` from timeline detail API
2. **"View Full Details" link** → Navigate to existing Risk/Blocker or Stakeholder detail view using the ID
3. **Reuse existing components** - The timeline detail data uses the same field names as the existing panels

```typescript
// Example: Clicking a risk/blocker timeline event
function handleTimelineEventClick(event: TimelineEvent) {
  if (isRiskBlockerEvent(event)) {
    // Option 1: Show inline preview (uses timeline detail data)
    showInlinePreview(event.event_data);

    // Option 2: Navigate to existing detail view
    const { risk_blocker_id } = event.event_data;
    router.push(`/deals/${event.deal_id}/risks-blockers/${risk_blocker_id}`);
  }

  if (isStakeholderEvent(event)) {
    const { stakeholder_id } = event.event_data;
    router.push(`/deals/${event.deal_id}/stakeholders/${stakeholder_id}`);
  }
}
```

### Risk/Blocker Events

These events track the lifecycle of deal risks and blockers:

| Event Type | When Created | Key Fields |
|------------|--------------|------------|
| `BLOCKER_ADDED` | Blocker created (manual or AI-extracted) | `severity`, `category`, `is_ai_generated` |
| `BLOCKER_RESOLVED` | Blocker marked as resolved | `severity`, `resolution_notes` |
| `BLOCKER_DISMISSED` | Blocker dismissed as not relevant | `severity` |
| `RISK_FLAGGED` | Risk identified (manual or AI-extracted) | `severity`, `category`, `is_ai_generated` |

**AI-Generated Indicator**: When `is_ai_generated: true`, the risk/blocker was automatically extracted by AI from meeting transcripts or emails. Display an AI badge or indicator.

**Linking to Risk/Blocker Details**: Use `risk_blocker_id` to link to the full risk/blocker detail view or API.

### Stakeholder Events

These events track stakeholder changes on the deal:

| Event Type | When Created | Key Fields |
|------------|--------------|------------|
| `STAKEHOLDER_ADDED` | Stakeholder added to deal | `email`, `name`, `job_title`, `source`, `stance` |
| `STAKEHOLDER_REMOVED` | Stakeholder removed from deal | `email`, `name` |

**Source Indicator**: The `source` field shows how the stakeholder was discovered:
- `CRM`: Synced from HubSpot/Salesforce contacts
- `CALENDAR_DISCOVERY`: Found in meeting attendees
- `EMAIL_DISCOVERY`: Found in email recipients
- `MANUAL`: Manually added by user

**Linking to Stakeholder Details**: Use `stakeholder_id` to link to the full stakeholder profile.

### Provider Icons

Backend includes `event_data.provider_logo_url` so clients can render provider icons/logos without hardcoding mappings.

- For communication and CRM events: `provider_logo_url` contains the URL
- For system events (risks, blockers, stakeholders): `provider_logo_url` is `null`

When `provider_logo_url` is `null`, render a custom icon based on `event_type`:
- `BLOCKER_ADDED` / `BLOCKER_RESOLVED` / `BLOCKER_DISMISSED`: Block or barrier icon
- `RISK_FLAGGED`: Warning or alert icon
- `STAKEHOLDER_ADDED` / `STAKEHOLDER_REMOVED`: Person or users icon

### Type Guards

```typescript
function isEmailEvent(event: TimelineEvent): event is TimelineEvent & { event_data: EmailEventData } {
  return event.event_type === "EMAIL";
}

function isMeetingEvent(event: TimelineEvent): event is TimelineEvent & { event_data: MeetingEventData } {
  return event.event_type === "MEETING";
}

function isStageChangeEvent(event: TimelineEvent): event is TimelineEvent & { event_data: StageChangeEventData } {
  return event.event_type === "STAGE_CHANGE";
}

function isDealCreatedEvent(event: TimelineEvent): event is TimelineEvent & { event_data: DealCreatedEventData } {
  return event.event_type === "DEAL_CREATED";
}

function isBlockerAddedEvent(event: TimelineEvent): event is TimelineEvent & { event_data: BlockerAddedEventData } {
  return event.event_type === "BLOCKER_ADDED";
}

function isBlockerResolvedEvent(event: TimelineEvent): event is TimelineEvent & { event_data: BlockerResolvedEventData } {
  return event.event_type === "BLOCKER_RESOLVED";
}

function isBlockerDismissedEvent(event: TimelineEvent): event is TimelineEvent & { event_data: BlockerDismissedEventData } {
  return event.event_type === "BLOCKER_DISMISSED";
}

function isRiskFlaggedEvent(event: TimelineEvent): event is TimelineEvent & { event_data: RiskFlaggedEventData } {
  return event.event_type === "RISK_FLAGGED";
}

function isStakeholderAddedEvent(event: TimelineEvent): event is TimelineEvent & { event_data: StakeholderAddedEventData } {
  return event.event_type === "STAKEHOLDER_ADDED";
}

function isStakeholderRemovedEvent(event: TimelineEvent): event is TimelineEvent & { event_data: StakeholderRemovedEventData } {
  return event.event_type === "STAKEHOLDER_REMOVED";
}

// Helper for risk/blocker events
function isRiskBlockerEvent(event: TimelineEvent): boolean {
  return ["BLOCKER_ADDED", "BLOCKER_RESOLVED", "BLOCKER_DISMISSED", "RISK_FLAGGED"].includes(event.event_type);
}

// Helper for stakeholder events
function isStakeholderEvent(event: TimelineEvent): boolean {
  return ["STAKEHOLDER_ADDED", "STAKEHOLDER_REMOVED"].includes(event.event_type);
}

// Helper for system events (no provider logo)
function isSystemEvent(event: TimelineEvent): boolean {
  return event.event_source === "SYSTEM";
}
```

### Source IDs for Subsequent API Calls

Timeline events include source IDs that must be used when calling other APIs:

| Event Type | Source ID Field | Used For |
|------------|-----------------|----------|
| EMAIL | `event_data.email_id` | Fetching full email details |
| MEETING | `event_data.calendar_event_id` | Meeting summary, prep, followup job APIs |
| BLOCKER_* / RISK_FLAGGED | `event_data.risk_blocker_id` | Risk/blocker detail API |
| STAKEHOLDER_* | `event_data.stakeholder_id` | Stakeholder profile API |

**Important**: Do NOT use `TimelineEvent.id` (which is `timeline_event_id`) when calling other APIs. Always use the appropriate source ID from `event_data`.

### Caching Strategy

Since list and detail share the same shape, frontend can:

1. Cache list items
2. On click, fetch detail and merge enriched `event_data` into cached item
3. No separate data structures needed

**Events requiring detail fetch for full data:**
- `EMAIL`: Fetch detail for body content, attachments, full recipient list
- `MEETING`: Fetch detail for attendee list, meeting summary
- `BLOCKER_*` / `RISK_FLAGGED`: Fetch detail for title, description, quote, owner, status
- `STAKEHOLDER_*`: Fetch detail for engagement metrics, decision power, temperature

**Events with all data in list response (no detail fetch needed):**
- `STAGE_CHANGE`: All data in metadata
- `DEAL_CREATED`: All data in metadata

### Rendering Example

```typescript
// Severity badge colors
const severityColors: Record<string, string> = {
  LOW: "bg-gray-100 text-gray-800",
  MEDIUM: "bg-yellow-100 text-yellow-800",
  HIGH: "bg-orange-100 text-orange-800",
  CRITICAL: "bg-red-100 text-red-800",
};

// Stance badge colors
const stanceColors: Record<string, string> = {
  CHAMPION: "bg-green-100 text-green-800",
  SUPPORTER: "bg-blue-100 text-blue-800",
  NEUTRAL: "bg-gray-100 text-gray-800",
  DETRACTOR: "bg-orange-100 text-orange-800",
  BLOCKER: "bg-red-100 text-red-800",
  UNKNOWN: "bg-gray-50 text-gray-500",
};

// List view
function TimelineListItem({ event }: { event: TimelineEvent }) {
  // Communication events
  if (isMeetingEvent(event)) {
    const { title, duration_minutes, attendee_count, provider_logo_url } = event.event_data;
    return (
      <div className="flex items-center gap-2">
        {provider_logo_url && <img src={provider_logo_url} className="w-5 h-5" />}
        <span>{title} • {duration_minutes} min • {attendee_count} attendees</span>
      </div>
    );
  }

  if (isEmailEvent(event)) {
    const { subject, snippet, from_email, provider_logo_url } = event.event_data;
    return (
      <div className="flex items-center gap-2">
        {provider_logo_url && <img src={provider_logo_url} className="w-5 h-5" />}
        <span>{subject} • {from_email}</span>
        <span className="text-gray-500 truncate">{snippet}</span>
      </div>
    );
  }

  // CRM lifecycle events
  if (isStageChangeEvent(event)) {
    const { old_stage_label, new_stage_label, provider_logo_url } = event.event_data;
    return (
      <div className="flex items-center gap-2">
        {provider_logo_url && <img src={provider_logo_url} className="w-5 h-5" />}
        <span className="stage-badge">{old_stage_label} → {new_stage_label}</span>
      </div>
    );
  }

  if (isDealCreatedEvent(event)) {
    const { deal_name, company_name, amount, currency, provider_logo_url } = event.event_data;
    return (
      <div className="flex items-center gap-2">
        {provider_logo_url && <img src={provider_logo_url} className="w-5 h-5" />}
        <strong>Deal Created:</strong> {deal_name}
        {company_name && <span> • {company_name}</span>}
        {amount && <span> • {formatCurrency(amount, currency)}</span>}
      </div>
    );
  }

  // Risk/Blocker events
  if (isBlockerAddedEvent(event)) {
    const { severity, category, is_ai_generated } = event.event_data;
    return (
      <div className="flex items-center gap-2">
        <BlockIcon className="w-5 h-5 text-red-500" />
        <span>Blocker Added</span>
        <span className={`badge ${severityColors[severity || "MEDIUM"]}`}>{severity}</span>
        {category && <span className="text-gray-500">{category}</span>}
        {is_ai_generated && <AiBadge />}
      </div>
    );
  }

  if (isBlockerResolvedEvent(event)) {
    const { severity } = event.event_data;
    return (
      <div className="flex items-center gap-2">
        <CheckCircleIcon className="w-5 h-5 text-green-500" />
        <span>Blocker Resolved</span>
        <span className={`badge ${severityColors[severity || "MEDIUM"]}`}>{severity}</span>
      </div>
    );
  }

  if (isBlockerDismissedEvent(event)) {
    const { severity } = event.event_data;
    return (
      <div className="flex items-center gap-2">
        <XCircleIcon className="w-5 h-5 text-gray-400" />
        <span>Blocker Dismissed</span>
        <span className={`badge ${severityColors[severity || "MEDIUM"]}`}>{severity}</span>
      </div>
    );
  }

  if (isRiskFlaggedEvent(event)) {
    const { severity, category, is_ai_generated } = event.event_data;
    return (
      <div className="flex items-center gap-2">
        <AlertTriangleIcon className="w-5 h-5 text-yellow-500" />
        <span>Risk Flagged</span>
        <span className={`badge ${severityColors[severity || "MEDIUM"]}`}>{severity}</span>
        {category && <span className="text-gray-500">{category}</span>}
        {is_ai_generated && <AiBadge />}
      </div>
    );
  }

  // Stakeholder events
  if (isStakeholderAddedEvent(event)) {
    const { name, email, job_title, source, stance } = event.event_data;
    return (
      <div className="flex items-center gap-2">
        <UserPlusIcon className="w-5 h-5 text-blue-500" />
        <span>Stakeholder Added: {name || email}</span>
        {job_title && <span className="text-gray-500">{job_title}</span>}
        {stance && stance !== "UNKNOWN" && (
          <span className={`badge ${stanceColors[stance]}`}>{stance}</span>
        )}
        <span className="text-xs text-gray-400">{source}</span>
      </div>
    );
  }

  if (isStakeholderRemovedEvent(event)) {
    const { name, email } = event.event_data;
    return (
      <div className="flex items-center gap-2">
        <UserMinusIcon className="w-5 h-5 text-gray-400" />
        <span>Stakeholder Removed: {name || email}</span>
      </div>
    );
  }

  return <div>Unknown event type: {event.event_type}</div>;
}
```

### Grouping Insights Events (Recommended)

When multiple risk/blocker or stakeholder events occur on the same day, the timeline can become cluttered. We recommend grouping these "insights events" into collapsible sections.

**Grouping Strategy:**

1. **Risk/Blocker Events** - Group all risk/blocker event types together:
   - `BLOCKER_ADDED`, `BLOCKER_RESOLVED`, `BLOCKER_DISMISSED`, `RISK_FLAGGED`
   - Show as: "3 Risk/Blocker Events" (collapsed)
   - On expand: Show all individual events with their details

2. **Stakeholder Events** - Group all stakeholder event types together:
   - `STAKEHOLDER_ADDED`, `STAKEHOLDER_REMOVED`
   - Show as: "4 Stakeholder Events" (collapsed)
   - On expand: Show all individual events with their details

3. **Communication & CRM Events** - Show individually (not grouped):
   - `EMAIL`, `MEETING`, `STAGE_CHANGE`, `DEAL_CREATED`

```typescript
// Group events by date and category
interface GroupedTimelineDay {
  date: string;
  communications: TimelineEvent[];      // EMAIL, MEETING
  crmLifecycle: TimelineEvent[];        // STAGE_CHANGE, DEAL_CREATED
  riskBlockerEvents: TimelineEvent[];   // All BLOCKER_* and RISK_FLAGGED
  stakeholderEvents: TimelineEvent[];   // All STAKEHOLDER_*
}

function groupEventsByDay(events: TimelineEvent[]): GroupedTimelineDay[] {
  const dayMap = new Map<string, GroupedTimelineDay>();

  for (const event of events) {
    const date = event.event_timestamp.split("T")[0];

    if (!dayMap.has(date)) {
      dayMap.set(date, {
        date,
        communications: [],
        crmLifecycle: [],
        riskBlockerEvents: [],
        stakeholderEvents: [],
      });
    }

    const day = dayMap.get(date)!;

    if (isRiskBlockerEvent(event)) {
      day.riskBlockerEvents.push(event);
    } else if (isStakeholderEvent(event)) {
      day.stakeholderEvents.push(event);
    } else if (event.event_type === "EMAIL" || event.event_type === "MEETING") {
      day.communications.push(event);
    } else {
      day.crmLifecycle.push(event);
    }
  }

  return Array.from(dayMap.values()).sort(
    (a, b) => new Date(b.date).getTime() - new Date(a.date).getTime()
  );
}

// Collapsible group component
function InsightsEventGroup({
  events,
  label,
  icon,
}: {
  events: TimelineEvent[];
  label: string;
  icon: React.ReactNode;
}) {
  const [expanded, setExpanded] = useState(false);

  if (events.length === 0) return null;

  // Single event: show directly without collapse
  if (events.length === 1) {
    return <TimelineListItem event={events[0]} />;
  }

  // Multiple events: show collapsible group
  return (
    <div className="border rounded-lg">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-2 p-3 hover:bg-gray-50"
      >
        {icon}
        <span className="font-medium">
          {events.length} {label}
        </span>
        <ChevronIcon className={expanded ? "rotate-180" : ""} />
      </button>

      {expanded && (
        <div className="border-t divide-y">
          {events.map((event) => (
            <div key={event.id} className="p-3">
              <TimelineListItem event={event} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// Timeline day component with grouped insights
function TimelineDaySection({ day }: { day: GroupedTimelineDay }) {
  return (
    <div className="mb-6">
      <h3 className="text-sm font-semibold text-gray-500 mb-3">
        {formatDate(day.date)}
      </h3>

      {/* Communications - show individually */}
      {day.communications.map((event) => (
        <TimelineListItem key={event.id} event={event} />
      ))}

      {/* CRM Lifecycle - show individually */}
      {day.crmLifecycle.map((event) => (
        <TimelineListItem key={event.id} event={event} />
      ))}

      {/* Risk/Blocker events - grouped & collapsible */}
      <InsightsEventGroup
        events={day.riskBlockerEvents}
        label="Risk/Blocker Events"
        icon={<AlertTriangleIcon className="w-5 h-5 text-yellow-500" />}
      />

      {/* Stakeholder events - grouped & collapsible */}
      <InsightsEventGroup
        events={day.stakeholderEvents}
        label="Stakeholder Events"
        icon={<UsersIcon className="w-5 h-5 text-blue-500" />}
      />
    </div>
  );
}
```

**Summary Badge (Optional):**

For collapsed groups, you can show a quick summary:

```typescript
// Risk/Blocker group summary
function RiskBlockerGroupSummary({ events }: { events: TimelineEvent[] }) {
  const added = events.filter((e) =>
    ["BLOCKER_ADDED", "RISK_FLAGGED"].includes(e.event_type)
  ).length;
  const resolved = events.filter((e) => e.event_type === "BLOCKER_RESOLVED").length;
  const dismissed = events.filter((e) => e.event_type === "BLOCKER_DISMISSED").length;

  return (
    <span className="text-sm text-gray-500">
      {added > 0 && <span className="text-red-600">+{added} new</span>}
      {resolved > 0 && <span className="text-green-600 ml-2">✓{resolved} resolved</span>}
      {dismissed > 0 && <span className="text-gray-400 ml-2">×{dismissed} dismissed</span>}
    </span>
  );
}

// Stakeholder group summary
function StakeholderGroupSummary({ events }: { events: TimelineEvent[] }) {
  const added = events.filter((e) => e.event_type === "STAKEHOLDER_ADDED").length;
  const removed = events.filter((e) => e.event_type === "STAKEHOLDER_REMOVED").length;

  return (
    <span className="text-sm text-gray-500">
      {added > 0 && <span className="text-blue-600">+{added} added</span>}
      {removed > 0 && <span className="text-gray-400 ml-2">-{removed} removed</span>}
    </span>
  );
}
```

---

## Notes

- `excluded_at != null` means the event is soft-hidden; show only if user opts to view excluded events.
- `cursor` is opaque; pass it unchanged to fetch the next page. When `null`, no more pages exist.
- Default sort is `desc` (newest first).
- System events (risks, blockers, stakeholders) have `event_source: "SYSTEM"` and `provider_logo_url: null`.
- AI-generated risks/blockers are marked with `is_ai_generated: true` in the event data.
