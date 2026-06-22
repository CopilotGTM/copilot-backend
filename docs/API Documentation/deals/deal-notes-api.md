# Deal Notes API

This API manages notes associated with CRM deals. Notes can be synced from external CRMs or created by users inside the application.

**Base URL**: `/users/crm/deals`

---

## Contract Summary

Notes are backed by `crm_notes`; the activity table is only a feed projection.

Public API naming is note-native:

| API field | Source | Writeable | Description |
| --- | --- | --- | --- |
| `title` | `crm_notes.title` | Yes | Optional user-facing note title |
| `body` | `crm_notes.body` | Yes | Required note body |
| `display_title` | Derived | No | `title` when present, otherwise the first 30 characters of normalized `body` |

Do not send `display_title` in create/update requests. The backend computes it so the frontend does not need to decide fallback behavior.

Legacy names `content` and `subject` are not part of this API contract.

---

## Response Envelope

All endpoints return the shared `GenericResponse` envelope:

```json
{
  "status": "SUCCESS | ERROR",
  "message": "Human-readable message",
  "data": {}
}
```

---

## Authentication And Rate Limits

- Bearer token required: `Authorization: Bearer <access_token>`
- Organisation and user are inferred from the token
- Reads use the standard user-read limit
- Writes use the standard user-write limit

---

## Data Shapes

```ts
export type UUID = string;
export type NoteSource = "user" | "hubspot" | "salesforce";

export interface NoteAuthor {
  id: string | null;
  name: string | null;
  email: string | null;
}

export interface NoteMetadata {
  synced_at: string | null;
}

export interface DealNoteItem {
  note_id: UUID;
  deal_id: UUID;
  title: string | null;
  body: string;
  display_title: string | null;
  created_at: string;
  updated_at: string | null;
  source: NoteSource;
  is_user_created: boolean;
  author: NoteAuthor;
  metadata: NoteMetadata | null;
}

export interface DealParticipant {
  email: string;
  name: string | null;
}

export interface DealNotesPage {
  items: DealNoteItem[];
  cursor: string | null;
  total_count: number;
  participants: DealParticipant[];
}

export interface CreateNoteRequest {
  body: string;
  title?: string | null;
}

export interface UpdateNoteRequest {
  body?: string | null;
  title?: string | null;
}
```

Validation:

| Field | Create | Update | Constraints |
| --- | --- | --- | --- |
| `body` | Required | Optional | 1-10000 characters after trim; cannot be blank |
| `title` | Optional | Optional | Max 500 characters after trim; blank becomes `null` |

---

## 1. Get Deal Notes

```http
GET /{deal_id}/notes
```

### Query Parameters

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `limit` | integer | 50 | Maximum notes per page, 1-100 |
| `cursor` | string | `null` | Opaque cursor from the previous response |
| `sort` | string | `desc` | `asc` or `desc` by note timestamp |

### Success Response

```json
{
  "status": "SUCCESS",
  "message": "Notes retrieved successfully",
  "data": {
    "items": [
      {
        "note_id": "0f4f5e3b-6e4a-4a79-8d29-3f3e5c4cfa31",
        "deal_id": "a8a3b1db-4d9a-4f3c-a3f6-2a5c1d8b9e1f",
        "title": null,
        "body": "Customer requested updated pricing for enterprise tier.",
        "display_title": "Customer requested updated",
        "created_at": "2026-03-24T10:35:00Z",
        "updated_at": "2026-03-24T10:35:00Z",
        "source": "user",
        "is_user_created": true,
        "author": {
          "id": "b1a7f9c0-1b2c-4f5a-9c1d-8b7c6a5d4e3f",
          "name": "John Smith",
          "email": "john.smith@company.com"
        },
        "metadata": {
          "synced_at": null
        }
      }
    ],
    "cursor": null,
    "total_count": 1,
    "participants": [
      {
        "email": "owner@company.com",
        "name": "Deal Owner"
      }
    ]
  }
}
```

`participants` contains the deal owner and stakeholders for note editor mention/autocomplete workflows.

### Error Responses

| Code | Description |
| --- | --- |
| `400` | Invalid cursor format |
| `404` | Deal not found or not accessible in this organisation |
| `422` | Invalid path or query parameter |
| `429` | Rate limit exceeded |
| `500` | Internal server error |

---

## 2. Create Deal Note

```http
POST /{deal_id}/notes
```

Creates a user note on the deal. The note is attributed to the authenticated user.

If an active user-created note with the same `body` and `title` already exists on the deal, the API returns that existing note with `200` and does not create a duplicate.

### Request Body

```json
{
  "title": "Budget Confirmation",
  "body": "Customer confirmed they have budget approval for Q1."
}
```

### Success Response

```json
{
  "status": "SUCCESS",
  "message": "Note created successfully",
  "data": {
    "note_id": "9c9f7b2c-8f12-4ef7-a52c-2b2b7b6d1b53",
    "deal_id": "a8a3b1db-4d9a-4f3c-a3f6-2a5c1d8b9e1f",
    "title": "Budget Confirmation",
    "body": "Customer confirmed they have budget approval for Q1.",
    "display_title": "Budget Confirmation",
    "created_at": "2026-03-24T10:40:00Z",
    "updated_at": "2026-03-24T10:40:00Z",
    "source": "user",
    "is_user_created": true,
    "author": {
      "id": "b1a7f9c0-1b2c-4f5a-9c1d-8b7c6a5d4e3f",
      "name": "John Smith",
      "email": "john.smith@company.com"
    },
    "metadata": {
      "synced_at": null
    }
  }
}
```

### Duplicate Response

```json
{
  "status": "SUCCESS",
  "message": "Identical note already exists; duplicate was not created",
  "data": {
    "note_id": "9c9f7b2c-8f12-4ef7-a52c-2b2b7b6d1b53",
    "deal_id": "a8a3b1db-4d9a-4f3c-a3f6-2a5c1d8b9e1f",
    "title": "Budget Confirmation",
    "body": "Customer confirmed they have budget approval for Q1.",
    "display_title": "Budget Confirmation",
    "created_at": "2026-03-24T10:40:00Z",
    "updated_at": "2026-03-24T10:40:00Z",
    "source": "user",
    "is_user_created": true,
    "author": {
      "id": "b1a7f9c0-1b2c-4f5a-9c1d-8b7c6a5d4e3f",
      "name": "John Smith",
      "email": "john.smith@company.com"
    },
    "metadata": {
      "synced_at": null
    }
  }
}
```

### Validation Error Example

```json
{
  "detail": [
    {
      "type": "value_error",
      "loc": ["body", "body"],
      "msg": "Body cannot be empty or whitespace-only",
      "input": "   "
    }
  ]
}
```

---

## 3. Update Deal Note

```http
PATCH /{deal_id}/notes/{note_id}
```

Updates a user-created note. External CRM notes are read-only.

### Request Body

Send any subset of writeable fields:

```json
{
  "title": "Updated Budget Confirmation",
  "body": "Updated with new pricing context from finance."
}
```

### Success Response

Returns `DealNoteItem`.

### Error Responses

| Code | Description |
| --- | --- |
| `400` | No fields to update or `body` is null |
| `404` | Deal or note not found |
| `409` | External CRM notes are read-only |
| `422` | Invalid payload or path parameter |
| `429` | Rate limit exceeded |
| `500` | Internal server error |

---

## 4. Delete Deal Note

```http
DELETE /{deal_id}/notes/{note_id}
```

Soft deletes a user-created note. External CRM notes are read-only.

### Success Response

Returns the deleted `DealNoteItem`.

### Error Responses

| Code | Description |
| --- | --- |
| `404` | Deal or note not found |
| `409` | External CRM notes are read-only |
| `422` | Invalid path parameter |
| `429` | Rate limit exceeded |
| `500` | Internal server error |

---

## Frontend Integration Notes

- Render `note.title` when showing the stored title field.
- Render `note.display_title` for compact list labels; it is always backend-derived.
- Send `body` and optional `title` on create/update.
- Do not send `content`, `subject`, or `display_title`.
- Use `is_user_created` or the mutation response status to gate edit/delete affordances; CRM-synced notes are read-only.
