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

type TimelineEventType = "EMAIL" | "MEETING" | "STAGE_CHANGE" | "DEAL_CREATED";

type TimelineEventSource =
  | "EMAIL_GMAIL"
  | "EMAIL_OUTLOOK"
  | "CALENDAR_GOOGLE"
  | "CALENDAR_OUTLOOK"
  | "CRM_HUBSPOT"
  | "CRM_SALESFORCE";

// ============================================================================
// Unified Timeline Event (same shape for list and detail)
// ============================================================================

export interface TimelineEvent {
  id: UUID;                            // timeline_event_id
  deal_id: UUID;
  event_type: TimelineEventType;
  event_source: TimelineEventSource;
  event_timestamp: string;             // ISO8601
  event_data: EmailEventData | MeetingEventData | StageChangeEventData | DealCreatedEventData;
  excluded_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

// ============================================================================
// Event Data Types (shape varies by event_type)
// ============================================================================

// EMAIL event_data
export interface EmailEventData {
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
  - Stage Change: `old_stage`, `new_stage`, `old_stage_label`, `new_stage_label`, plus deal context
  - Deal Created: All fields (no enrichment needed)
- **Detail**: Same preview fields **plus** enriched fields
  - Email: adds `from_name`, `to_emails`, `cc_emails`, `thread_id`, `body_text`, `body_html`, `body_truncated`, `body_length`, `has_attachments`, `attachments`, `received_at`, `sent_at`, `direction`
  - Meeting: adds `attendees[]`, `meeting_summary`
  - Stage Change: Same as list (all data included)
  - Deal Created: Same as list (all data included)

### Stage Change & Deal Created Events

These CRM lifecycle events include **deal and company context directly** in the response. This eliminates the need for separate API calls to fetch deal/company details.

**Stage Change** includes:
- Stage transition: `old_stage` → `new_stage` with optional display labels
- Deal context: `deal_name`, `company_name`, `amount`, `currency`, `pipeline_name`

**Deal Created** includes:
- Deal info: `deal_name`, `initial_stage`, `pipeline_name`, `amount`, `currency`
- Company info: `company_name`, `company_domain`
- Owner info: `owner_name`
- Primary contact: `primary_contact_name`, `primary_contact_email`

**Note**: These events don't require detail enrichment—all data is returned in both list and detail views.

### Email Body Content

The detail endpoint returns full email body content:

- **`body_text`**: Plain text version of the email body
- **`body_html`**: HTML version of the email body (if available)
- **`body_truncated`**: `true` if the body exceeded 50,000 characters and was truncated
- **`body_length`**: Original length of the body in characters (useful for UI indicators)

**Rendering recommendation**: Prefer `body_html` for rich display, fall back to `body_text` for plain display or when HTML is unavailable. Always sanitize HTML before rendering.

### Email Attachments

When `has_attachments` is `true`, the `attachments` array contains metadata:

```typescript
interface EmailAttachment {
  name: string | null;      // Filename
  size: number | null;      // Size in bytes
  mime_type: string | null; // MIME type (e.g., "application/pdf")
}
```

**Note**: Attachment download is not currently supported via this API. The metadata is provided for display purposes.

### Email Direction

The `direction` field indicates email flow relative to the deal:

- `"INBOUND"`: Email received from external contacts
- `"OUTBOUND"`: Email sent to external contacts

### Provider Icons

Backend includes `event_data.provider_logo_url` so clients can render provider icons/logos without hardcoding mappings.

- `provider_logo_url` is derived from `event_source` (provider), so each event returns at most one provider logo.
- If a provider is unknown/unmapped, `provider_logo_url` is `null` (clients can render a generic fallback).

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
```

### Caching Strategy

Since list and detail share the same shape, frontend can:

1. Cache list items
2. On click, fetch detail and merge enriched `event_data` into cached item
3. No separate data structures needed
4. **For STAGE_CHANGE and DEAL_CREATED**: No detail fetch needed—all data is in the list response

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
  if (isStageChangeEvent(event)) {
    const { old_stage_label, new_stage_label, company_name } = event.event_data;
    return (
      <div>
        <span className="stage-badge">{old_stage_label} → {new_stage_label}</span>
        {company_name && <span className="company">{company_name}</span>}
      </div>
    );
  }
  if (isDealCreatedEvent(event)) {
    const { deal_name, company_name, amount, currency } = event.event_data;
    return (
      <div>
        <strong>Deal Created:</strong> {deal_name}
        {company_name && <span> • {company_name}</span>}
        {amount && <span> • {formatCurrency(amount, currency)}</span>}
      </div>
    );
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
  if (isEmailEvent(event)) {
    const {
      subject, from_email, from_name, to_emails,
      body_html, body_text, body_truncated,
      attachments, direction
    } = event.event_data;
    return (
      <div>
        <span className={direction === "INBOUND" ? "badge-inbound" : "badge-outbound"}>
          {direction}
        </span>
        <h2>{subject}</h2>
        <p>From: {from_name || from_email}</p>
        <p>To: {to_emails?.join(", ")}</p>
        {body_html ? (
          <div dangerouslySetInnerHTML={{ __html: sanitizeHtml(body_html) }} />
        ) : (
          <pre>{body_text}</pre>
        )}
        {body_truncated && <p className="warning">Email body truncated...</p>}
        {attachments?.map(att => (
          <div key={att.name}>{att.name} ({formatBytes(att.size)})</div>
        ))}
      </div>
    );
  }
  if (isStageChangeEvent(event)) {
    const {
      old_stage_label, new_stage_label,
      deal_name, company_name, amount, currency, pipeline_name
    } = event.event_data;
    return (
      <div className="stage-change-detail">
        <h2>Stage Change</h2>
        <div className="stage-transition">
          <span className="old-stage">{old_stage_label}</span>
          <span className="arrow">→</span>
          <span className="new-stage">{new_stage_label}</span>
        </div>
        <div className="deal-context">
          <p><strong>Deal:</strong> {deal_name}</p>
          {company_name && <p><strong>Company:</strong> {company_name}</p>}
          {amount && <p><strong>Value:</strong> {formatCurrency(amount, currency)}</p>}
          {pipeline_name && <p><strong>Pipeline:</strong> {pipeline_name}</p>}
        </div>
      </div>
    );
  }
  if (isDealCreatedEvent(event)) {
    const {
      deal_name, initial_stage, pipeline_name, amount, currency,
      company_name, company_domain, owner_name,
      primary_contact_name, primary_contact_email
    } = event.event_data;
    return (
      <div className="deal-created-detail">
        <h2>Deal Created</h2>
        <div className="deal-info">
          <p><strong>Deal:</strong> {deal_name}</p>
          {amount && <p><strong>Value:</strong> {formatCurrency(amount, currency)}</p>}
          {initial_stage && <p><strong>Initial Stage:</strong> {initial_stage}</p>}
          {pipeline_name && <p><strong>Pipeline:</strong> {pipeline_name}</p>}
        </div>
        {(company_name || company_domain) && (
          <div className="company-info">
            <h3>Company</h3>
            {company_name && <p>{company_name}</p>}
            {company_domain && <p className="domain">{company_domain}</p>}
          </div>
        )}
        {owner_name && (
          <div className="owner-info">
            <h3>Owner</h3>
            <p>{owner_name}</p>
          </div>
        )}
        {(primary_contact_name || primary_contact_email) && (
          <div className="contact-info">
            <h3>Primary Contact</h3>
            {primary_contact_name && <p>{primary_contact_name}</p>}
            {primary_contact_email && <p className="email">{primary_contact_email}</p>}
          </div>
        )}
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
