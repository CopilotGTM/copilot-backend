# Chat API Documentation

## Base URL

```text
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
  job_id: UUID;
  user_message_id: UUID;
  assistant_message_id: UUID;
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

### Create Chat Endpoint

```http
POST /users/chats/create_chat
```

### Create Chat Description

Creates a new chat session owned by the current user in their organisation.

```json
{
  "title": "My First Chat",
  "tags": ["alpha", "demo"]
}
```

### Create Chat Success Response (200)

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

### Create Chat Error Responses

- 400 Bad Request — "Title cannot be null or empty", "Title cannot exceed 100 characters"
- 500 Internal Server Error — "Unable to create chat session"

### Create Chat Example cURL

```bash
curl -X POST "$BASE_URL/users/chats/create_chat" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title":"My First Chat","tags":["alpha","demo"]}'
```

---

## 2) Delete Chat (soft delete)

### Delete Chat Endpoint

```http
DELETE /users/chats/delete_chat
```

### Delete Chat Description

Soft-deletes a chat session owned by the current user.

```json
{ "session_id": "f3c8c3f2-1f5a-4a6f-94e4-7a7f0e05e2a0" }
```

### Delete Chat Success Response (200)

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

### Delete Chat Error Responses

- 404 Not Found — "Chat session not found"
- 500 Internal Server Error — "Unable to delete chat session"

---

## 3) Rename Chat

### Rename Chat Endpoint

```http
PATCH /users/chats/rename_chat
```

```json
{ "session_id": "<UUID>", "title": "New Title" }
```

### Rename Chat Success Response (200)

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

### Rename Chat Error Responses

- 400 Bad Request — invalid title
- 404 Not Found — "Chat session not found"
- 500 Internal Server Error — "Unable to rename chat session"

---

## 4) List Chats (paginated)

### List Chats Endpoint

```http
GET /users/chats/list_chats?limit=20&cursor=<opaque>
```

### List Chats Description

Returns your chat sessions ordered by recency. Use `cursor` from the previous response to fetch the next page.

### List Chats Success Response (200)

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

### List Chats Error Responses

- 500 Internal Server Error — "Unable to list chat sessions"

---

## 5) Search Chats (typeahead)

### Search Chats Endpoint

```http
GET /users/chats/search_chats?q=<text>&limit=10
```

### Search Chats Description

Searches chat sessions by title. Best-effort, not paginated; intended for typeahead.

### Search Chats Success Response (200)

Same shape as List Chats, with `cursor: null`.

### Search Chats Error Responses

- 500 Internal Server Error — "Unable to search chat sessions"

---

## 6) Toggle Favorite

### Toggle Favorite Endpoint

```http
PATCH /users/chats/favorite_chat
```

```json
{ "session_id": "<UUID>", "favorite": true }
```

### Toggle Favorite Success Response (200)

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

### Toggle Favorite Error Responses

- 404 Not Found — "Chat session not found"
- 400 Bad Request — validation issues
- 500 Internal Server Error — "Unable to update favorite state"

---

## 7) Send Message

### Send Message Endpoint

```http
POST /users/chats/send_message
```

### Send Message Description

Persists a user message (with optional attachments) and enqueues a background job to generate the assistant reply.

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

### Send Message Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Message accepted",
  "data": {
    "job_id": "0f49b5d8-...",
    "user_message_id": "f1b9e0a8-...",
    "assistant_message_id": "a2c7d1e9-..."
  }
}
```

Notes

- `job_id`: Use this to connect to the SSE stream at `/message_events`
- `user_message_id`: The persisted ID of the user's message
- `assistant_message_id`: Pre-generated ID for the assistant's response (message will be created by worker)

### Send Message Error Responses

- 400 Bad Request — invalid content, role not `user`, or session not found
- 500 Internal Server Error — scheduling/processing issue

### Send Message Example cURL

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

### Message Events Endpoint

```http
GET /users/chats/message_events?job_id=<UUID>
```

### Message Events Description

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

### SSE Semantics (Worker, Delta, Ordering)

- Producer and delivery
  - Frames originate from the chat worker processing the job. The worker publishes structured frames to a per-job Redis Stream; the API's SSE endpoint relays those frames to the client.
  - Delivery uses Redis consumer groups for consume-once semantics by default, minimizing duplicates. Legacy replay mode is used when consumer groups are unavailable.

- Base frame schema (always included)
  - `job_id`: the job identifier (string) for SSE stream correlation
  - `assistant_message_id`: the pre-generated ID for the assistant message being created
  - `version`: frame schema version (e.g., "1.0")
  - `timestamp`: float seconds since epoch
  - `sequence`: 0-based, monotonically increasing per job
  - `type`: one of the known frame types listed above

- Ordering and resume
  - Ordering: frames are emitted in order; use `sequence` (preferred) or the SSE `id` to ensure monotonic processing.
  - Resume: pass `Last-Event-ID` on reconnect. In consume-once mode, the server tracks position; in legacy mode, the server replays from the provided ID.
  - Heartbeats/Timeouts: you will receive `ping` heartbeats ~25s. After ~15 minutes idle, a `timeout` with `{ "reconnect": true }` is sent — reconnect with backoff.

- Content streaming contract (delta vs replace)
  - `answer.delta`: append-only partial content. Render incrementally by concatenating `content` chunks in order.
  - `answer.replace`: full current answer. If received, treat this as the canonical text and replace the accumulated buffer.
  - Finalization: a `finalize` frame is followed by an `event: complete`. After `complete`, close the stream and optionally refresh with `GET /messages/list` to show the persisted assistant message.

- Diagnostics / “autopsy” frames (what the frontend can expect)
  - `answer.metrics`: confidence signal for the answer (e.g., `very_low|low|medium|high|very_high`). May also include `citations` in some cases.
  - `sources.replace`: full replacement list of sources/citations to display alongside the answer.
  - `suggestions.replace`: full replacement list of suggested follow-ups.
  - `progress.update`: background status such as `{ stage, message?, progress [0..1], eta_seconds? }` to drive progress UI.
  - `tool.usage`: instrumentation about tools used `{ tool_name, status, duration_ms?, result_summary? }`.
  - `thinking.update`: optional thinking/planning breadcrumbs `{ thought, stage }` for debug or dev UIs.
  - `agent.switch` and `question.prompt`: advanced flows for agent handoff or asking the user for more info.

- Frontend handling (practical expectations)
  - Stream only from `answer.delta` (append) and apply `answer.replace` when present to ensure the UI shows the canonical answer.
  - Merge diagnostics frames live: update confidence, sources, suggestions, progress bars, and tool/thinking panes as frames arrive.
  - Stop on `event: complete` (or on `finalize` shortly followed by `complete`). On `timeout` or `error` where `reconnect` is true, reconnect with backoff and include `Last-Event-ID`.
  - After completion, call `GET /users/chats/messages/list` for the session to render the stored assistant message and any enriched metadata.

### Frontend Handling Tips

- Use `answer.delta` to append and `answer.replace` to overwrite the current answer.
- Update confidence, suggestions, sources, and progress bars from corresponding frames.
- Stop streaming when you receive an `event: complete` (or a `finalize` frame followed by `complete`).

### Minimal Browser Example

```js
// After sending message, you receive: { job_id, user_message_id, assistant_message_id }
// Create assistant message placeholder in UI with assistant_message_id

const src = new EventSource(`/users/chats/message_events?job_id=${jobId}`, {
  withCredentials: true
});

src.addEventListener('ready', () => console.log('ready'));
src.addEventListener('ping', () => {/* keep-alive */});
src.addEventListener('frame', (e) => {
  const frame = JSON.parse(e.data);
  // frame.job_id - correlate with your request
  // frame.assistant_message_id - update this message in your UI
  
  switch(frame.type){
    case 'answer.delta': 
      // Append frame.content to assistant message
      updateMessage(frame.assistant_message_id, { content: append(frame.content) });
      break;
    case 'answer.replace': 
      // Replace full content
      updateMessage(frame.assistant_message_id, { content: frame.content });
      break;
    case 'suggestions.replace':
      // Update suggestions
      updateMessage(frame.assistant_message_id, { suggestions: frame.items });
      break;
    case 'sources.replace':
      // Update sources
      updateMessage(frame.assistant_message_id, { sources: frame.sources });
      break;
    case 'finalize': 
      // Mark complete
      updateMessage(frame.assistant_message_id, { status: 'completed' });
      break;
  }
});
src.addEventListener('complete', () => src.close());
```

---

## 9) List Messages (cursor-based)

### List Messages Endpoint

```http
GET /users/chats/messages/list?session_id=<UUID>&limit=20&cursor=<opaque>&role=<role>&message_type=<type>
```

### List Messages Description

Returns chat messages for a session with lightweight enrichment (attachments, sources, suggestions, confidence).

### List Messages Success Response (200)

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

### List Messages Error Responses

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
