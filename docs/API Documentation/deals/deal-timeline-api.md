# Deal Timeline API

This API returns the unified, denormalized timeline for a deal (emails and meetings). Both list and detail endpoints return the same `TimelineEvent` shape—the detail endpoint simply returns enriched `event_data`.

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

type TimelineEventType = "EMAIL" | "MEETING";

type TimelineEventSource =
  | "EMAIL_GMAIL"
  | "EMAIL_OUTLOOK"
  | "CALENDAR_GOOGLE"
  | "CALENDAR_OUTLOOK";

// ============================================================================
// Unified Timeline Event (same shape for list and detail)
// ============================================================================

export interface TimelineEvent {
  id: UUID;                            // timeline_event_id
  deal_id: UUID;
  event_type: TimelineEventType;
  event_source: TimelineEventSource;
  event_timestamp: string;             // ISO8601
  event_data: EmailEventData | MeetingEventData;
  excluded_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

// ============================================================================
// Event Data Types (shape varies by event_type)
// ============================================================================

// EMAIL event_data
export interface EmailEventData {
  // --- List Preview (always present) ---
  subject: string | null;
  snippet: string | null;
  from_email: string | null;

  // --- Detail Enrichment (populated on detail fetch) ---
  to_emails?: string[] | null;
  cc_emails?: string[] | null;
  thread_id?: string | null;
  has_attachments?: boolean | null;
}

// MEETING event_data
export interface MeetingEventData {
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

// ============================================================================
// Pagination
// ============================================================================

export interface TimelinePage {
  deal_id: UUID;
  items: TimelineEvent[];
  total_count: number;
  cursor: string | null;               // opaque pagination cursor
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
| `event_types` | string | Comma-separated filter, e.g. `EMAIL,MEETING` |
| `start_date` | datetime | Include events after this timestamp |
| `end_date` | datetime | Include events before this timestamp |
| `include_excluded` | boolean (default false) | When true, include soft-hidden events |
| `sort` | `asc` \| `desc` (default `desc`) | Sort order |

### Sample Request

```http
GET /users/crm/deals/6c3f98d5-2e6b-4a1c-8f8c-2e8f0211b0b9/timeline?limit=20&sort=desc
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

## 2) Get Timeline Event Detail

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
      "subject": "Re: Proposal Review",
      "snippet": "Thanks for sending over the proposal. I've reviewed it with the team...",
      "from_email": "jane@acme.com",
      "to_emails": ["john@company.com"],
      "cc_emails": ["sales@company.com"],
      "thread_id": "AAQkAbc123...",
      "has_attachments": false
    },
    "excluded_at": null,
    "created_at": "2025-01-14T15:30:00Z",
    "updated_at": "2025-01-14T15:30:00Z"
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
- **Detail**: Same preview fields **plus** enriched fields
  - Email: adds `to_emails`, `cc_emails`, `thread_id`, `has_attachments`
  - Meeting: adds `attendees[]`, `meeting_summary`

### Type Guards

```typescript
function isEmailEvent(event: TimelineEvent): event is TimelineEvent & { event_data: EmailEventData } {
  return event.event_type === "EMAIL";
}

function isMeetingEvent(event: TimelineEvent): event is TimelineEvent & { event_data: MeetingEventData } {
  return event.event_type === "MEETING";
}
```

### Caching Strategy

Since list and detail share the same shape, frontend can:
1. Cache list items
2. On click, fetch detail and merge enriched `event_data` into cached item
3. No separate data structures needed

### Rendering Example

```typescript
// List view
function TimelineListItem({ event }: { event: TimelineEvent }) {
  if (isMeetingEvent(event)) {
    const { title, duration_minutes, attendee_count } = event.event_data;
    return <div>{title} • {duration_minutes} min • {attendee_count} attendees</div>;
  }
  if (isEmailEvent(event)) {
    const { subject, snippet, from_email } = event.event_data;
    return <div>{subject} • {from_email} • {snippet}</div>;
  }
}

// Detail view
function TimelineDetail({ event }: { event: TimelineEvent }) {
  if (isMeetingEvent(event)) {
    const { attendees, meeting_summary } = event.event_data;
    return (
      <div>
        <h2>{event.event_data.title}</h2>
        <p>Attendees: {attendees?.join(", ")}</p>
        {meeting_summary?.data && <MeetingSummaryPanel data={meeting_summary.data} />}
      </div>
    );
  }
}
```

---

## Notes

- `excluded_at != null` means the event is soft-hidden; show only if user opts to view excluded events.
- `cursor` is opaque; pass it unchanged to fetch the next page. When `null`, no more pages exist.
- Default sort is `desc` (newest first).
