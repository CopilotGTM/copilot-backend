# Deal Notes API

This API manages notes associated with CRM deals. Notes can be either synced from CRM systems (HubSpot/Salesforce) or created manually by users within the application.

**Base URL**: `/users/crm/deals`

---

## Overview

### What are Deal Notes?

| Source | Description | Example |
| --- | --- | --- |
| **CRM-synced** | Notes imported from HubSpot or Salesforce | "Customer requested updated pricing proposal" |
| **User-created** | Notes added manually via this API | "Discussed migration timeline on call" |

### How Sources are Distinguished

| Field | CRM-synced | User-created |
| --- | --- | --- |
| `integration_id` | Set (UUID) | `null` |
| `is_user_created` | `false` | `true` |
| `source` | `"hubspot"` or `"salesforce"` | `"user"` |

---

## Response Envelope

All endpoints return the shared `GenericResponse` envelope:

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
- Organisation/user are inferred from the token; clients never pass these explicitly

---

## Rate Limiting

| Operation | Limit |
| --- | --- |
| Read (`GET`) | ~60/min, 1000/hour per user |
| Write (`POST`/`PATCH`/`DELETE`) | ~30/min, 500/hour per user |

On limit breach: `429 Too Many Requests` with `Retry-After` header.

---

## Data Shapes (TypeScript)

```ts
export type UUID = string;

export type NoteSource = "user" | "hubspot" | "salesforce";

export interface NoteAuthor {
  id: string;
  name: string | null;
  email: string | null;
}

export interface NoteMetadata {
  external_id: string | null;     // provider note id for CRM-synced notes
  synced_at: string | null;       // ISO8601
}

export interface DealNoteItem {
  note_id: UUID;
  deal_id: UUID;
  content: string;
  subject: string | null;
  created_at: string;             // ISO8601
  updated_at: string | null;      // ISO8601
  source: NoteSource;
  is_user_created: boolean;
  author: NoteAuthor;
  metadata: NoteMetadata | null;
}

export interface DealParticipant {
  email: string;                  // participant email address
  name: string | null;            // participant name
}

export interface DealNotesPage {
  items: DealNoteItem[];
  cursor: string | null;          // opaque pagination cursor
  total_count: number;
  participants: DealParticipant[]; // deal owner + stakeholders for @mentions
}

export interface CreateNoteRequest {
  content: string;                // required, 1..10000 chars
  subject?: string | null;        // optional, <= 500 chars
}
```

---

## 1. Get Deal Notes

Retrieves all notes associated with a specific deal, including both CRM-synced and user-created notes.

### Endpoint (Get Deal Notes)

```http
GET /{deal_id}/notes
```

### Path Parameters (Get Deal Notes)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `deal_id` | UUID | Yes | The unique identifier of the deal |

### Query Parameters (Get Deal Notes)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | integer | 50 | Maximum notes per page (1-100) |
| `cursor` | string | null | Opaque cursor from previous response |
| `sort` | string | "desc" | Sort order by created date (`asc` or `desc`) |

### Success Response (200) (Get Deal Notes)

```json
{
  "status": "SUCCESS",
  "message": "Notes retrieved successfully",
  "data": {
    "items": [
      {
        "note_id": "0f4f5e3b-6e4a-4a79-8d29-3f3e5c4cfa31",
        "deal_id": "a8a3b1db-4d9a-4f3c-a3f6-2a5c1d8b9e1f",
        "content": "Customer requested updated pricing for enterprise tier.",
        "subject": null,
        "created_at": "2025-12-24T10:35:00Z",
        "updated_at": "2025-12-24T10:35:00Z",
        "source": "user",
        "is_user_created": true,
        "author": {
          "id": "b1a7f9c0-1b2c-4f5a-9c1d-8b7c6a5d4e3f",
          "name": "John Smith",
          "email": "john.smith@company.com"
        },
        "metadata": {
          "external_id": null,
          "synced_at": null
        }
      },
      {
        "note_id": "1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d",
        "deal_id": "a8a3b1db-4d9a-4f3c-a3f6-2a5c1d8b9e1f",
        "content": "Discussed technical requirements with IT team. They need SSO integration.",
        "subject": "Technical Discovery Notes",
        "created_at": "2025-12-20T14:22:00Z",
        "updated_at": "2025-12-20T14:22:00Z",
        "source": "hubspot",
        "is_user_created": false,
        "author": {
          "id": "12345678",
          "name": "Sales Rep",
          "email": "rep@company.com"
        },
        "metadata": {
          "external_id": "hs_note_987654321",
          "synced_at": "2025-12-20T15:00:00Z"
        }
      }
    ],
    "cursor": "eyJ0cyI6IjIwMjUtMTItMjBUMTQ6MjI6MDBaIiwiaWQiOiIxYTJiM2M0ZC01ZTZmLTdhOGItOWMwZC0xZTJmM2E0YjVjNmQifQ==",
    "total_count": 15,
    "participants": [
      {
        "email": "owner@company.com",
        "name": "Deal Owner"
      },
      {
        "email": "stakeholder1@customer.com",
        "name": "Jane Doe"
      },
      {
        "email": "stakeholder2@customer.com",
        "name": "Bob Smith"
      }
    ]
  }
}
```

> **Note:** The `participants` array contains the deal owner and all stakeholders associated with the deal. This can be used for @mention autocomplete in note editors.

### Error Responses (Get Deal Notes)

| Code | Description |
|------|-------------|
| `400` | Invalid cursor format |
| `422` | Invalid query or path parameter (e.g., `limit`, `sort`, `deal_id`) |
| `404` | Deal not found or not accessible in this organisation |
| `429` | Rate limit exceeded |
| `500` | Internal server error |

### Usage Example (Get Deal Notes)

```ts
const baseUrl = '/users/crm/deals';

// Fetch first page
const response = await fetch(`${baseUrl}/${dealId}/notes?limit=20&sort=desc`, {
  headers: { Authorization: `Bearer ${token}` }
});
const { data } = await response.json();

// Fetch next page using cursor
if (data.cursor) {
  const nextPage = await fetch(
    `${baseUrl}/${dealId}/notes?limit=20&cursor=${data.cursor}`,
    { headers: { Authorization: `Bearer ${token}` } }
  );
}
```

---

## 2. Create Deal Note

Creates a new user note for a specific deal. The note is attributed to the authenticated user.

### Endpoint (Create Deal Note)

```http
POST /{deal_id}/notes
```

### Path Parameters (Create Deal Note)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `deal_id` | UUID | Yes | The unique identifier of the deal |

### Request Body (Create Deal Note)

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `content` | string | Yes | 1-10000 chars, non-empty | Note body text |
| `subject` | string | No | Max 500 chars | Optional title/subject |

### Request Example (Create Deal Note)

```json
{
  "content": "Customer confirmed they have budget approval for Q1. Ready to move forward with POC.",
  "subject": "Budget Confirmation"
}
```

### Success Response (201) (Create Deal Note)

```json
{
  "status": "SUCCESS",
  "message": "Note created successfully",
  "data": {
    "note_id": "9c9f7b2c-8f12-4ef7-a52c-2b2b7b6d1b53",
    "deal_id": "a8a3b1db-4d9a-4f3c-a3f6-2a5c1d8b9e1f",
    "content": "Customer confirmed they have budget approval for Q1. Ready to move forward with POC.",
    "subject": "Budget Confirmation",
    "created_at": "2025-12-24T10:40:00Z",
    "updated_at": "2025-12-24T10:40:00Z",
    "source": "user",
    "is_user_created": true,
    "author": {
      "id": "b1a7f9c0-1b2c-4f5a-9c1d-8b7c6a5d4e3f",
      "name": null,
      "email": null
    },
    "metadata": {
      "external_id": null,
      "synced_at": null
    }
  }
}
```

### Error Responses (Create Deal Note)

| Code | Description |
|------|-------------|
| `422` | Validation error (invalid UUID or body constraints) |
| `404` | Deal not found or not accessible in this organisation |
| `429` | Rate limit exceeded |
| `500` | Internal server error |

### Validation Error Example (Create Deal Note)

```json
{
  "detail": [
    {
      "type": "value_error",
      "loc": ["body", "content"],
      "msg": "Content cannot be empty or whitespace-only",
      "input": "   "
    }
  ]
}
```

### Usage Example (Create Deal Note)

```ts
const baseUrl = '/users/crm/deals';
const response = await fetch(`${baseUrl}/${dealId}/notes`, {
  method: 'POST',
  headers: {
    'Authorization': `Bearer ${token}`,
    'Content-Type': 'application/json'
  },
  body: JSON.stringify({
    content: 'Discussed pricing with CFO. They want volume discounts.',
    subject: 'Pricing Discussion'
  })
});

const { data: createdNote } = await response.json();
```

---

## 3. Update Deal Note

Updates a user-created note for a specific deal. External CRM notes are read-only.

### Endpoint (Update Deal Note)

```http
PATCH /{deal_id}/notes/{note_id}
```

### Path Parameters (Update Deal Note)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `deal_id` | UUID | Yes | The unique identifier of the deal |
| `note_id` | UUID | Yes | The unique identifier of the note |

### Request Body (Update Deal Note)

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `content` | string | No | 1-10000 chars, non-empty | Updated note body |
| `subject` | string | No | Max 500 chars | Updated title/subject |

### Request Example (Update Deal Note)

```json
{
  "content": "Updated with new pricing context from finance."
}
```

### Success Response (200) (Update Deal Note)

```json
{
  "status": "SUCCESS",
  "message": "Note updated successfully",
  "data": {
    "note_id": "9c9f7b2c-8f12-4ef7-a52c-2b2b7b6d1b53",
    "deal_id": "a8a3b1db-4d9a-4f3c-a3f6-2a5c1d8b9e1f",
    "content": "Updated with new pricing context from finance.",
    "subject": "Budget Confirmation",
    "created_at": "2025-12-24T10:40:00Z",
    "updated_at": "2025-12-24T10:55:00Z",
    "source": "user",
    "is_user_created": true,
    "author": {
      "id": "b1a7f9c0-1b2c-4f5a-9c1d-8b7c6a5d4e3f",
      "name": null,
      "email": null
    },
    "metadata": {
      "external_id": null,
      "synced_at": null
    }
  }
}
```

### Error Responses (Update Deal Note)

| Code | Description |
|------|-------------|
| `400` | No fields to update or content cannot be null |
| `422` | Validation error (invalid UUID or body constraints) |
| `404` | Deal or note not found |
| `409` | External CRM notes are read-only |
| `429` | Rate limit exceeded |
| `500` | Internal server error |

---

## 4. Delete Deal Note

Soft deletes a user-created note for a specific deal. External CRM notes are read-only.

### Endpoint (Delete Deal Note)

```http
DELETE /{deal_id}/notes/{note_id}
```

### Path Parameters (Delete Deal Note)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `deal_id` | UUID | Yes | The unique identifier of the deal |
| `note_id` | UUID | Yes | The unique identifier of the note |

### Success Response (200) (Delete Deal Note)

```json
{
  "status": "SUCCESS",
  "message": "Note deleted successfully",
  "data": {
    "note_id": "9c9f7b2c-8f12-4ef7-a52c-2b2b7b6d1b53",
    "deal_id": "a8a3b1db-4d9a-4f3c-a3f6-2a5c1d8b9e1f",
    "content": "Updated with new pricing context from finance.",
    "subject": "Budget Confirmation",
    "created_at": "2025-12-24T10:40:00Z",
    "updated_at": "2025-12-24T10:55:00Z",
    "source": "user",
    "is_user_created": true,
    "author": {
      "id": "b1a7f9c0-1b2c-4f5a-9c1d-8b7c6a5d4e3f",
      "name": null,
      "email": null
    },
    "metadata": {
      "external_id": null,
      "synced_at": null
    }
  }
}
```

### Error Responses (Delete Deal Note)

| Code | Description |
|------|-------------|
| `422` | Invalid path parameter (e.g., `deal_id`, `note_id`) |
| `404` | Deal or note not found |
| `409` | External CRM notes are read-only |
| `429` | Rate limit exceeded |
| `500` | Internal server error |

---

## Frontend Integration Guide

### Displaying Notes

```tsx
// Differentiate note sources with visual indicators
function NoteItem({ note }: { note: DealNoteItem }) {
  const sourceIcon = {
    user: <UserIcon />,
    hubspot: <HubSpotIcon className="text-orange-500" />,
    salesforce: <SalesforceIcon className="text-blue-500" />
  }[note.source];

  const sourceBadge = note.is_user_created 
    ? null 
    : <Badge>Synced from {note.source}</Badge>;

  return (
    <div className="note-item">
      <div className="note-header">
        {sourceIcon}
        <span className="author-name">{note.author.name || 'Unknown'}</span>
        {sourceBadge}
        <time>{formatDate(note.created_at)}</time>
      </div>
      {note.subject && <h4>{note.subject}</h4>}
      <p>{note.content}</p>
    </div>
  );
}
```

### Add Note Form

```tsx
function AddNoteForm({ dealId, onNoteAdded }) {
  const [content, setContent] = useState('');
  const [subject, setSubject] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setIsSubmitting(true);
    
    try {
      const response = await createDealNote(dealId, { content, subject });
      onNoteAdded(response.data);
      setContent('');
      setSubject('');
    } catch (error) {
      // Handle error
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <form onSubmit={handleSubmit}>
      <input 
        placeholder="Subject (optional)" 
        value={subject}
        onChange={(e) => setSubject(e.target.value)}
        maxLength={500}
      />
      <textarea 
        placeholder="Add a note..." 
        value={content}
        onChange={(e) => setContent(e.target.value)}
        maxLength={10000}
        required
      />
      <button type="submit" disabled={isSubmitting || !content.trim()}>
        Add Note
      </button>
    </form>
  );
}
```

---

## Implementation Notes

- **Pagination**: Uses cursor-based pagination for stable results during concurrent updates
- **Sorting**: Timestamp is computed as `coalesce(source_created_at, created_at)` for null-safety
- **Author attribution**: For user-created notes, `author.id` is the internal user UUID; for CRM notes, it's the CRM owner ID
- **Edit/delete**: Only user-created notes (integration_id = null) can be edited or deleted; external CRM notes remain read-only
