# Deal Timeline API Response Design

## Overview

This document outlines a **unified response structure** for the Deal Timeline APIs. The goal is to keep the response shape consistent between the **list** and **detail** endpoints, where the detail endpoint simply returns an "enriched" version of the same structure.

---

## Design Principles

1. **Single Base Model**: Both list items and detail responses share the same top-level structure.
2. **Event Type Discriminator**: A `event_type` field at the top level indicates what kind of event this is (EMAIL, MEETING, MESSAGE, etc.).
3. **Typed Event Data**: An `event_data` object contains type-specific fields; its shape varies by `event_type`.
4. **Progressive Enrichment**: The detail endpoint returns the same model but with additional fields populated in `event_data`.
5. **Frontend Simplicity**: Frontend can reuse the same TypeScript interface for list items and detail; detail just has more optional fields filled in.

---

## Unified Response Structure

### Base Timeline Event Model

```typescript
interface TimelineEvent {
  // === Core Identity ===
  id: string;                          // timeline_event_id (UUID)
  deal_id: string;
  
  // === Event Classification ===
  event_type: "EMAIL" | "MEETING";
  event_source: string;                // e.g., "EMAIL_GMAIL", "CALENDAR_GOOGLE"
  
  // === Timing ===
  event_timestamp: string;             // ISO datetime - when the event occurred
  
  // === Type-Specific Data (contains ALL event-specific fields including title/participants) ===
  event_data: EmailEventData | MeetingEventData;
  
  // === Metadata ===
  excluded_at: string | null;          // Soft-hide timestamp
  created_at: string;
  updated_at: string;
}
```

---

## Event Data Types

### Email Event Data

```typescript
interface EmailEventData {
  // --- List Preview ---
  subject: string | null;
  snippet: string | null;              // First ~100 chars of body
  from_email: string | null;
  
  // --- Detail Enrichment (populated on detail fetch) ---
  to_emails?: string[];
  cc_emails?: string[];
  thread_id?: string;
  has_attachments?: boolean;
}
```

### Meeting Event Data

```typescript
interface MeetingEventData {
  // --- List Preview ---
  title: string | null;
  duration_minutes: number | null;
  attendee_count: number | null;
  
  // --- Detail Enrichment (populated on detail fetch) ---
  attendees?: string[];                // Full list of attendee emails
  
  // Meeting intelligence (from deal association)
  meeting_summary?: {
    status: "PENDING" | "PROCESSING" | "COMPLETED" | "FAILED" | null;
    data: object | null;
  };
}
```

---

## API Endpoints

### 1. List Timeline Events

```
GET /users/crm/deals/{deal_id}/timeline?limit=20&cursor=...
```

**Response:**
```json
{
  "success": true,
  "message": "Timeline events retrieved",
  "data": {
    "deal_id": "uuid",
    "items": [
      {
        "id": "timeline-event-uuid",
        "deal_id": "deal-uuid",
        "event_type": "MEETING",
        "event_source": "CALENDAR_GOOGLE",
        "event_timestamp": "2024-01-15T10:00:00Z",
        "event_data": {
          "title": "Q1 Planning Call with Acme Corp",
          "duration_minutes": 60,
          "attendee_count": 4
        },
        "excluded_at": null,
        "created_at": "2024-01-10T08:00:00Z",
        "updated_at": "2024-01-15T11:00:00Z"
      },
      {
        "id": "timeline-event-uuid-2",
        "deal_id": "deal-uuid",
        "event_type": "EMAIL",
        "event_source": "EMAIL_GMAIL",
        "event_timestamp": "2024-01-14T15:30:00Z",
        "event_data": {
          "subject": "Re: Proposal Review",
          "snippet": "Thanks for sending over the proposal. I've reviewed it with...",
          "from_email": "jane@acme.com"
        },
        "excluded_at": null,
        "created_at": "2024-01-14T15:30:00Z",
        "updated_at": "2024-01-14T15:30:00Z"
      }
    ],
    "total_count": 45,
    "cursor": "eyJvZmZzZXQiOiAyMH0="
  }
}
```

### 2. Get Timeline Event Detail

```
GET /users/crm/deals/timeline/{timeline_event_id}
```

**Response:** Same structure as list item, but `event_data` has more fields populated.

```json
{
  "success": true,
  "message": "Timeline event detail retrieved",
  "data": {
    "id": "timeline-event-uuid",
    "deal_id": "deal-uuid",
    "event_type": "MEETING",
    "event_source": "CALENDAR_GOOGLE",
    "event_timestamp": "2024-01-15T10:00:00Z",
    "event_data": {
      "title": "Q1 Planning Call with Acme Corp",
      "duration_minutes": 60,
      "attendee_count": 4,
      "attendees": ["john@company.com", "jane@acme.com", "bob@acme.com", "alice@acme.com"],
      "meeting_summary": {
        "status": "COMPLETED",
        "data": { "key_points": ["Budget approved"], "action_items": [] }
      }
    },
    "excluded_at": null,
    "created_at": "2024-01-10T08:00:00Z",
    "updated_at": "2024-01-15T11:00:00Z"
  }
}
```

---

## Frontend Integration

### Single TypeScript Interface

```typescript
// Frontend uses ONE interface for both list and detail
interface TimelineEvent {
  id: string;
  deal_id: string;
  event_type: "EMAIL" | "MEETING";
  event_source: string;
  event_timestamp: string;
  event_data: EmailEventData | MeetingEventData;
  excluded_at: string | null;
  created_at: string;
  updated_at: string;
}

// Type guard for rendering
function isMeetingEvent(event: TimelineEvent): event is TimelineEvent & { event_data: MeetingEventData } {
  return event.event_type === "MEETING";
}

function isEmailEvent(event: TimelineEvent): event is TimelineEvent & { event_data: EmailEventData } {
  return event.event_type === "EMAIL";
}
```

### Rendering Logic

```typescript
// List view - uses preview fields from event_data
function TimelineListItem({ event }: { event: TimelineEvent }) {
  if (isMeetingEvent(event)) {
    return (
      <div>
        <span>{event.event_data.title}</span>
        <span>{event.event_data.duration_minutes} min • {event.event_data.attendee_count} attendees</span>
      </div>
    );
  }
  if (isEmailEvent(event)) {
    return (
      <div>
        <span>{event.event_data.subject}</span>
        <span>{event.event_data.from_email}</span>
        <span>{event.event_data.snippet}</span>
      </div>
    );
  }
}

// Detail view - uses enriched fields (same interface, more data)
function TimelineDetail({ event }: { event: TimelineEvent }) {
  if (isMeetingEvent(event)) {
    const { meeting_summary, attendees } = event.event_data;
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

## Benefits

| Aspect | Benefit |
|--------|---------|
| **Consistency** | Same model shape for list and detail - no mental mapping |
| **Caching** | Frontend can cache list items and merge detail data |
| **Type Safety** | Single TypeScript interface with discriminated `event_type` |
| **Extensibility** | New event types just add a new `EventData` variant |
| **Performance** | List returns minimal data; detail fetches only on click |

---

## Implementation Checklist

- [ ] Update Pydantic models in `crm_models.py` to unified structure
- [ ] Update `DealTimelineService.get_timeline_events()` to return unified shape
- [ ] Update `DealTimelineService.get_timeline_event_detail()` to return enriched version
- [ ] Update handler responses to use new models
- [ ] Update API documentation

---

## Open Questions

1. **Should `event_data` be nullable?** For edge cases where source record is deleted but timeline entry exists.
2. **Include `source_record_id`?** Useful for debugging but exposes internal IDs.
3. **Flatten meeting intelligence?** Currently nested under `meeting_prep`, `meeting_summary`, `meeting_followup` - could flatten to top level of `event_data`.

---

*Awaiting approval before implementation.*
