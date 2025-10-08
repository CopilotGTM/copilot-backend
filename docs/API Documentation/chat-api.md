# Chat API Documentation

## Base URL
```
/users/chats
```

---

## Response Envelope

- All endpoints return a consistent envelope:
```json
{
  "status": "SUCCESS | ERROR",
  "message": "Human-readable message",
  "data": { ... } | null
}
```
- Timestamps use ISO 8601 with `Z` suffix (UTC), e.g. `2025-10-05T10:30:00Z`.

## Authentication
- Required — Bearer token
- Header: `Authorization: Bearer <access_token>`

## Rate Limiting (defaults)
- Write endpoints (POST/PATCH/DELETE): ~15/min, 100/hour per user
- Read endpoints (GET): ~60/min, 1000/hour per user
- On limit breach: `429 Too Many Requests` with `Retry-After` header

---

## Integration Overview

- For a unified flow with uploads and SSE, see: `docs/API Documentation/chat-apis-integration-diagram.md`

---

## Production Conventions

- Authentication: Bearer JWT in `Authorization: Bearer <token>`
- Content Type: `Content-Type: application/json`; `Accept: application/json`
- Idempotency: Create/send endpoints are not idempotent. Clients should guard against duplicate submissions (disable submit while in-flight; de-dupe using a client-generated UUID).
- Pagination: Cursor-based; treat `cursor` as opaque and pass back verbatim.
- Stability/Versioning: No explicit version path today. Non-breaking changes are additive; breaking changes will be announced.
- Time & Timestamps: ISO 8601 UTC with `Z` suffix.

### Rate Limits
- Write: ~15/min, 100/hour per user (configurable)
- Read: ~60/min, 1000/hour per user (configurable)
- Exceeding returns `429 Too Many Requests` with `Retry-After` header (seconds)

### Error Model
- Envelope always present with HTTP status aligned to error:
  - 400: invalid input (empty content/title, bad filters)
  - 401: missing/invalid token
  - 403: forbidden (invalid user context)
  - 404: resource not found (chat not found)
  - 409/422: validation/conflict (rare)
  - 429: rate limited
  - 500: unhandled server error

```json
{
  "status": "ERROR",
  "message": "Human-readable reason",
  "data": null
}
```

### Security & Tenancy
- All operations are evaluated against the authenticated `user_id` and `organisation_id`.
- Queries exclude soft-deleted items; writes are scoped to the caller.

### SSE Guidance (Message Events)
- Transport: Server-Sent Events; events: `ready`, `frame`, `ping`, `complete`, `timeout`, `error`.
- Timeouts/Heartbeats: timeout ~15 minutes; heartbeats ~25s. Reconnect with backoff on `timeout` or transient `error`.
- Resume: Provide `Last-Event-ID` header to resume (legacy replay mode supported).
- Auth caveat: Native `EventSource` cannot attach `Authorization` headers. Options:
  - Use an SSE polyfill that supports headers
  - Have a reverse proxy inject the header based on a secure cookie
  - Implement an app bridge that exchanges a cookie/session for an auth header

## Data Types

### Enums
- `ChatMessageRole`: `"user" | "assistant" | "system"`
- `ChatMessageType`: `"GENERAL_MESSAGE" | "TECHNICAL_DECK" | "RFP_SHEET"`

### Request Models (TypeScript)
```ts
type UUID = string;

export interface ChatSessionCreate {
  title: string;
  tags?: string[]; // optional labels
}

export interface DeleteChat {
  session_id: UUID;
}

export interface RenameChat {
  session_id: UUID;
  title: string; // 1..100 chars
}

export interface FavoriteChat {
  session_id: UUID;
  favorite: boolean;
}

export interface SendMessageRequest {
  session_id: UUID;
  content: string; // non-empty
  attachments?: UUID[]; // attachment_ids from Attachments API
  message_type?: ChatMessageType; // default: GENERAL_MESSAGE
  role: "user"; // must be "user"
  is_edited?: boolean; // default: false
  edited_message_id?: UUID; // required if is_edited === true
}
```

### Response Models (TypeScript)
```ts
export interface ChatSessionOut {
  session_id: UUID;
  title: string;
  chat_type: string; // e.g., "GENERAL_CHAT"
  is_favorite: boolean;
  tags: string[];
  created_at: string; // ISO8601
  updated_at: string; // ISO8601
}

export interface ChatSessionsPage {
  items: ChatSessionOut[];
  cursor?: string | null; // opaque
}

export interface SendMessageResponse {
  message_id: UUID;
  job_id: UUID;
}

export interface ChatAttachmentView {
  attachment_id: UUID;
  filename: string;
  mimetype: string;
  thumbnail_small_url?: string | null;
}

export interface ChatMessageListItem {
  message_id: UUID;
  session_id: UUID;
  role: ChatMessageRole;
  message_type: ChatMessageType;
  content: string;
  is_edited: boolean;
  created_at: string; // ISO8601
  updated_at: string; // ISO8601
  attachments: ChatAttachmentView[];
  confidence_score?: string | null; // "very_low"|"low"|"medium"|"high"|"very_high" or null
  sources?: Array<Record<string, any>> | null;
  suggestions?: string[] | null;
}

export interface ChatMessagesPage {
  items: ChatMessageListItem[];
  cursor?: string | null;
}

// Standard envelope used by all endpoints
export interface ApiEnvelope<T> {
  status: "SUCCESS" | "ERROR";
  message: string;
  data: T | null;
}
```

### SSE Event Frames (Message Events)
- Events: `ready`, `frame`, `ping`, `complete`, `timeout`, `error`
- `frame.data` examples:
```json
{ "type": "answer.delta", "content": "partial markdown ...", "format": "markdown", "sequence": 3 }
{ "type": "answer.replace", "content": "full answer ...", "format": "markdown" }
{ "type": "answer.metrics", "confidence": "high" }
{ "type": "suggestions.replace", "items": ["Follow-up A", "B"] }
{ "type": "sources.replace", "sources": [{"title":"Doc","url":"..."}] }
{ "type": "progress.update", "stage":"retrieval", "progress": 0.7 }
{ "type": "tool.usage", "tool_name":"search", "status":"ok" }
{ "type": "thinking.update", "thought": "...", "stage":"plan" }
{ "type": "finalize", "reason": "done" }
```

### Retry & Backoff Recommendations
- Non-idempotent writes (create_chat, send_message): do not auto-retry without user confirmation; prefer manual retry to avoid duplicates.
- Reads (list/search): retry with exponential backoff (200ms → 400ms → 800ms; jitter) up to a small cap.
- SSE: on `timeout`/network errors, reconnect with backoff (1s → 5s → 10s; cap 30s). Stop on `complete`.

---

## Security & Compliance (SOC 2 Alignment)
- Transport Security
  - Enforce TLS 1.2+; recommend HSTS at the edge. All API access over HTTPS only.
  - CORS: restrict in production to trusted origins.
- Authentication & Authorization
  - Bearer JWT on every call. Endpoints enforce user/org scoping for all reads/writes.
  - Prefer role-based authorization at the gateway (future extension) for admin vs user scopes.
- Input Validation & Integrity
  - Pydantic validation rejects empty content and invalid roles/types.
  - Cursor-based pagination guards against numeric enumeration; cursors are opaque.
  - SSE consumer groups provide exactly-once delivery semantics in consume-once mode for message jobs.
- Logging & Auditability
  - Log request outcomes and resource IDs (session_id, message_id, job_id) without storing content or tokens.
  - Redact potentially sensitive fields; apply log retention and access controls.
- Availability & Resilience
  - Per-user rate limiting (read/write budgets) with `429` + `Retry-After`.
  - SSE heartbeats and reconnect guidance provide graceful degradation on network interruptions.
- Privacy & Data Handling
  - Message content is application data; treat as confidential. Avoid logging content. Support future purge/retention policies at the org level.
- Change Management & Versioning
  - Document non-breaking additions; coordinate breaking changes. Maintain compatibility for event frames and payloads.

## 1) Create Chat

### Endpoint
```http
POST /users/chats/create_chat
```

### Description
Creates a new chat session owned by the current user in their organisation.

### Request Body
```json
{
  "title": "My First Chat",
  "tags": ["alpha", "demo"]
}
```

### Success Response (200)
```json
{
  "status": "SUCCESS",
  "message": "Chat created",
  "data": {
    "session_id": "f3c8c3f2-1f5a-4a6f-94e4-7a7f0e05e2a0",
    "title": "My First Chat",
    "chat_type": "GENERAL_CHAT",
    "is_favorite": false,
    "tags": ["alpha", "demo"],
    "created_at": "2025-10-06T08:12:00Z",
    "updated_at": "2025-10-06T08:12:00Z"
  }
}
```

### Error Responses
- 400 Bad Request — "Title cannot be null or empty", "Title cannot exceed 100 characters"
- 500 Internal Server Error — "Unable to create chat session"

### Example cURL
```bash
curl -X POST "$BASE_URL/users/chats/create_chat" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title":"My First Chat","tags":["alpha","demo"]}'
```

---

## 2) Delete Chat (soft delete)

### Endpoint
```http
DELETE /users/chats/delete_chat
```

### Description
Soft-deletes a chat session owned by the current user.

### Request Body
```json
{ "session_id": "f3c8c3f2-1f5a-4a6f-94e4-7a7f0e05e2a0" }
```

### Success Response (200)
```json
{
  "status": "SUCCESS",
  "message": "Chat deleted",
  "data": {
    "session_id": "f3c8c3f2-1f5a-4a6f-94e4-7a7f0e05e2a0",
    "title": "My First Chat",
    "chat_type": "GENERAL_CHAT",
    "is_favorite": false,
    "tags": [],
    "created_at": "2025-10-06T08:12:00Z",
    "updated_at": "2025-10-06T08:15:00Z"
  }
}
```

### Error Responses
- 404 Not Found — "Chat session not found"
- 500 Internal Server Error — "Unable to delete chat session"

---

## 3) Rename Chat

### Endpoint
```http
PATCH /users/chats/rename_chat
```

### Request Body
```json
{ "session_id": "<UUID>", "title": "New Title" }
```

### Success Response (200)
```json
{
  "status": "SUCCESS",
  "message": "Chat renamed",
  "data": {
    "session_id": "<UUID>",
    "title": "New Title",
    "chat_type": "GENERAL_CHAT",
    "is_favorite": false,
    "tags": [],
    "created_at": "2025-10-06T08:12:00Z",
    "updated_at": "2025-10-06T08:16:00Z"
  }
}
```

### Error Responses
- 400 Bad Request — invalid title
- 404 Not Found — "Chat session not found"
- 500 Internal Server Error — "Unable to rename chat session"

---

## 4) List Chats (paginated)

### Endpoint
```http
GET /users/chats/list_chats?limit=20&cursor=<opaque>
```

### Description
Returns your chat sessions ordered by recency. Use `cursor` from the previous response to fetch the next page.

### Success Response (200)
```json
{
  "status": "SUCCESS",
  "message": "Chats listed",
  "data": {
    "items": [
      {
        "session_id": "<UUID>",
        "title": "My First Chat",
        "chat_type": "GENERAL_CHAT",
        "is_favorite": false,
        "tags": [],
        "created_at": "2025-10-06T08:12:00Z",
        "updated_at": "2025-10-06T08:12:00Z"
      }
    ],
    "cursor": "<opaque-cursor-or-null>"
  }
}
```

### Error Responses
- 500 Internal Server Error — "Unable to list chat sessions"

---

## 5) Search Chats (typeahead)

### Endpoint
```http
GET /users/chats/search_chats?q=<text>&limit=10
```

### Description
Searches chat sessions by title. Best-effort, not paginated; intended for typeahead.

### Success Response (200)
Same shape as List Chats, with `cursor: null`.

### Error Responses
- 500 Internal Server Error — "Unable to search chat sessions"

---

## 6) Toggle Favorite

### Endpoint
```http
PATCH /users/chats/favorite_chat
```

### Request Body
```json
{ "session_id": "<UUID>", "favorite": true }
```

### Success Response (200)
```json
{
  "status": "SUCCESS",
  "message": "Chat favorited",
  "data": {
    "session_id": "<UUID>",
    "title": "...",
    "chat_type": "GENERAL_CHAT",
    "is_favorite": true,
    "tags": [],
    "created_at": "2025-10-06T08:12:00Z",
    "updated_at": "2025-10-06T08:18:00Z"
  }
}
```

### Error Responses
- 404 Not Found — "Chat session not found"
- 400 Bad Request — validation issues
- 500 Internal Server Error — "Unable to update favorite state"

---

## 7) Send Message

### Endpoint
```http
POST /users/chats/send_message
```

### Description
Persists a user message (with optional attachments) and enqueues a background job to generate the assistant reply.

### Request Body
```json
{
  "session_id": "<UUID>",
  "content": "How do I...",
  "attachments": ["<ATTACHMENT_UUID>"],
  "message_type": "GENERAL_MESSAGE",
  "role": "user",
  "is_edited": false,
  "edited_message_id": null
}
```
Notes
- `role` must be `user`.
- `attachments` are IDs from the Chat Attachments API once uploaded/finalized.

### Success Response (200)
```json
{
  "status": "SUCCESS",
  "message": "Message accepted",
  "data": {
    "message_id": "f1b9e0a8-...",
    "job_id": "0f49b5d8-..."
  }
}
```

### Error Responses
- 400 Bad Request — invalid content, role not `user`, or session not found
- 500 Internal Server Error — scheduling/processing issue

### Example cURL
```bash
curl -X POST "$BASE_URL/users/chats/send_message" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "session_id":"<UUID>",
        "content":"Show me recent updates",
        "attachments":[],
        "message_type":"GENERAL_MESSAGE",
        "role":"user"
      }'
```

---

## 8) Message Events (SSE)

### Endpoint
```http
GET /users/chats/message_events?job_id=<UUID>
```

### Description
Streams Server-Sent Events for a specific message job. Includes reconnection support via `Last-Event-ID`.

### Event Types
- `ready` — stream initialized
- `frame` — data payload (see `type` field below)
- `ping` — heartbeat
- `complete` — job finished (client may close the stream)
- `timeout` — stream timed out; reconnect allowed
- `error` — internal stream error; reconnect allowed

### Frame `data` (representative examples)
```json
{ "type": "answer.delta", "content": "partial markdown ...", "format": "markdown", "sequence": 3 }
{ "type": "answer.replace", "content": "full answer ...", "format": "markdown" }
{ "type": "answer.metrics", "confidence": "high" }
{ "type": "suggestions.replace", "items": ["Follow-up A", "B"] }
{ "type": "sources.replace", "sources": [{"title":"Doc","url":"..."}] }
{ "type": "progress.update", "stage":"retrieval", "progress": 0.7 }
{ "type": "tool.usage", "tool_name":"search", "status":"ok" }
{ "type": "thinking.update", "thought": "...", "stage":"plan" }
{ "type": "finalize", "reason": "done" }
```

### Frontend Handling Tips
- Use `answer.delta` to append and `answer.replace` to overwrite the current answer.
- Update confidence, suggestions, sources, and progress bars from corresponding frames.
- Stop streaming when you receive an `event: complete` (or a `finalize` frame followed by `complete`).

### Minimal Browser Example
```js
const src = new EventSource(`/users/chats/message_events?job_id=${jobId}`, {
  withCredentials: true
});
src.addEventListener('ready', () => console.log('ready'));
src.addEventListener('ping', () => {/* keep-alive */});
src.addEventListener('frame', (e) => {
  const frame = JSON.parse(e.data);
  switch(frame.type){
    case 'answer.delta': /* append */ break;
    case 'answer.replace': /* replace */ break;
    case 'finalize': /* will get complete next */ break;
  }
});
src.addEventListener('complete', () => src.close());
```

---

## 9) List Messages (cursor-based)

### Endpoint
```http
GET /users/chats/messages/list?session_id=<UUID>&limit=20&cursor=<opaque>&role=<role>&message_type=<type>
```

### Description
Returns chat messages for a session with lightweight enrichment (attachments, sources, suggestions, confidence).

### Success Response (200)
```json
{
  "status": "SUCCESS",
  "message": "Messages listed",
  "data": {
    "items": [
      {
        "message_id": "<UUID>",
        "session_id": "<UUID>",
        "role": "assistant",
        "message_type": "GENERAL_MESSAGE",
        "content": "Rendered text",
        "is_edited": false,
        "created_at": "2025-10-06T08:12:00Z",
        "updated_at": "2025-10-06T08:12:00Z",
        "attachments": [
          {
            "attachment_id": "<UUID>",
            "filename": "file.pdf",
            "mimetype": "application/pdf",
            "thumbnail_small_url": "https://...sas..."
          }
        ],
        "confidence_score": "high",
        "sources": [{"title":"Doc","url":"..."}],
        "suggestions": ["Next question A"]
      }
    ],
    "cursor": null
  }
}
```

### Error Responses
- 400 Bad Request — invalid session or filters
- 500 Internal Server Error — "Unable to list messages"

---

## Implementation Notes (Frontend)
- Always include the Bearer token header.
- Typical flow:
  1) `POST /create_chat`
  2) [Optional] Upload attachments via Chat Attachments API (see separate doc)
  3) `POST /send_message` (include attachment IDs)
  4) Open SSE `/message_events?job_id=...` and render frames in real time
  5) Refresh UI via `GET /messages/list` when complete to show persisted assistant message
- Use pagination cursors as opaque strings; pass them back verbatim.
