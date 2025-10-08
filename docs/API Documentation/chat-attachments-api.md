# Chat Attachments API Documentation

## Base URL
```
/users/chats/upload
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

## Authentication
- Required — Bearer token
- Header: `Authorization: Bearer <access_token>`

## Rate Limiting (defaults)
- Write endpoints (POST): ~15/min, 100/hour per user
- Read endpoints (GET): ~60/min, 1000/hour per user
- On limit breach: `429 Too Many Requests` with `Retry-After` header

---

## Integration Overview

- For a unified chat + attachments flow (including SSE), see: `docs/API Documentation/chat-apis-integration-diagram.md`

---

## Production Conventions

- Authentication: Bearer JWT in `Authorization: Bearer <token>`
- Content Type: `Content-Type: application/json`; `Accept: application/json`
- Upload Model: Direct-to-storage via short-lived SAS URL; server never proxies file bytes.
- Idempotency: Presign/finalize are not idempotent. Avoid automatic retries without user confirmation.
- Timestamps: ISO 8601 UTC with `Z` suffix.

### Lifecycle & States
- `pending_upload` → `uploaded` → `processing` → `completed | failed`
- `finalize` transitions from `pending_upload` to `processing` and enqueues background work.
- `download` allowed only when `status = completed`.

### Rate Limits
- Write: ~15/min, 100/hour per user (configurable)
- Read: ~60/min, 1000/hour per user (configurable)
- Exceeding returns `429 Too Many Requests` with `Retry-After` header

### Error Model
- 400: invalid mimetype/size, invalid state (e.g., download before `completed`)
- 401: missing/invalid token
- 404: not found or not owned by user/org
- 429: rate limited
- 500: internal/unexpected error

```json
{
  "status": "ERROR",
  "message": "Human-readable reason",
  "data": null
}
```

### SSE Guidance (Attachment Stream)
- Events: `status` (array of attachments), `ping`, `complete`, `timeout`, `error`.
- Scope: If `attachment_ids` omitted, server streams all non-terminal attachments for the user.
- Timeouts/Heartbeats: default timeout ~30 minutes; heartbeats ~25s; reconnect with backoff on `timeout`/transient `error`.
- Auth caveat: Native `EventSource` cannot attach headers; use a polyfill, reverse proxy that injects the header from a cookie, or an app-layer bridge.

### Retry & Backoff Recommendations
- Presign/Finalize: do not auto-retry blindly; prompt the user and re-issue once.
- Download URL: safe to retry on network errors with exponential backoff.
- SSE: reconnect on `timeout`/network errors with capped backoff (1s → 5s → 10s; max 30s).

### Security & Tenancy
- All attachment operations are validated against the authenticated `user_id` and `organisation_id`.
- Batch operations keep partial successes when possible; failures are reported per item.

### Security & Compliance (SOC 2 Alignment)
- Transport Security
  - Enforce TLS 1.2+; recommend HSTS at the edge. Never serve over plaintext HTTP.
  - CORS restricted to trusted origins in deployment (dev uses wide-open for local).
- Storage Security
  - Attachments stored in private containers with server-side encryption at rest (cloud-managed keys by default; CMK/KMS recommended for higher assurance).
  - No public ACLs; access only via time-bound SAS URLs.
  - SAS URLs are path-scoped and short-lived (upload ~15m; download 30–120m). Clients must not persist SAS URLs in logs or databases.
- Authentication & Authorization
  - Bearer JWT required for all endpoints. Every read/write verified against tenant (`organisation_id`) and owner (`user_id`).
  - Follow least privilege: presign grants write to a single blob; download grants read to a single blob.
- Input Validation & Content Controls
  - MIME type allowlist and strict max size enforced server-side; reject on violation.
  - Finalize enforces valid state transitions (e.g., cannot download before `completed`).
  - Background antivirus/inspection pipeline (see `virus_scan_status`) marks/blocks infected files and publishes failure.
- Secrets & Key Management
  - Service credentials are loaded from a secret manager; keys are never embedded in code or responses.
  - Rotate credentials regularly; restrict access via least privilege.
- Logging & Auditability
  - Do not log raw SAS URLs, tokens, or user content. Log only non-sensitive identifiers (`attachment_id`, status, timings, error codes). Redact PII where applicable.
  - Emit audit events for presign/finalize/download (who, what, when, org/user, outcome).
- Data Retention & Deletion
  - Attachment model includes `retention_policy`, `is_deleted`, `deleted_at`. Enforce org policy for retention and secure deletion. Avoid indefinite retention of failed uploads.
- Availability & Resilience
  - Rate limiting protects against abuse (429 + Retry-After). SSE includes keep-alives and reconnection guidance.
  - Dependencies: Redis (pub/sub) and Cloud Storage. Operate with health checks/alerts; document incident response paths outside this spec.

## Data Types

### Enums
- `AttachmentStatus`: `"pending_upload" | "uploaded" | "processing" | "completed" | "failed"`

### Request/Response Models (TypeScript)
```ts
type UUID = string;

// Requests
export interface PresignRequest {
  filename: string; // 1..255 chars
  mimetype: string; // must be allowed by config
  size_bytes: number; // > 0 and <= MAX_UPLOAD_FILE_SIZE_BYTES
}

export interface FinalizeRequest {
  attachment_id: UUID;
  checksum?: string | null; // optional
}

export interface AttachmentBatchPresignRequest {
  files: PresignRequest[]; // length >= 1
}

export interface AttachmentBatchFinalizeRequest {
  files: FinalizeRequest[]; // length >= 1
}

// Single presign response
export interface PresignResponse {
  attachment_id: UUID;
  filename: string;
  mimetype: string;
  upload_url: string; // HttpUrl
  size_bytes: number;
  expires_in_seconds: number; // e.g., 900
  status: AttachmentStatus; // usually "pending_upload"
  success: true;
  error_message: null;
}

// Finalized attachment (minimal)
export interface UploadedAttachmentResponse {
  attachment_id: UUID;
  uploaded_at: string; // ISO8601
  status: AttachmentStatus; // processing|completed|failed
}

// Download URL
export interface AttachmentDownloadResponse {
  attachment_id: UUID;
  download_url: string; // HttpUrl
  expires_in_seconds: number; // default ~1800
}

// Batch presign per-file
export interface AttachmentPresignFileResponse {
  attachment_id?: UUID | null;
  filename: string;
  mimetype?: string | null;
  upload_url?: string | null;
  size_bytes?: number | null;
  success: boolean;
  error_message?: string | null;
  expires_in_seconds: number; // 0 if failed
  status?: AttachmentStatus | null; // "pending_upload" on success; "failed" on failure
}

export interface AttachmentBatchPresignResponse {
  files: AttachmentPresignFileResponse[];
  errors: Array<Record<string, any>>; // diagnostic items (index, filename, reason, error_message)
}

// Batch finalize
export interface AttachmentBatchFinalizeResult {
  attachment_id: UUID;
  status?: string | null; // server may return status string
  error?: string | null;
  updated_at?: string | null; // ISO8601
}

export interface AttachmentBatchFinalizeResponse {
  results: AttachmentBatchFinalizeResult[];
  errors: Array<Record<string, any>>;
}

// Standard envelope used by all endpoints
export interface ApiEnvelope<T> {
  status: "SUCCESS" | "ERROR";
  message: string;
  data: T | null;
}
```

### SSE Event Frames (Attachment Stream)
- Events: `status`, `ping`, `complete`, `timeout`, `error`
- `status.data` payload shapes:
```json
[
  {
    "attachment_id": "...",
    "status": "processing",
    "updated_at": "2025-10-06T08:12:15Z",
    "error_message": null,
    "thumbnail_small_url": null
  }
]
```

---

## 1) Presign Single Upload

### Endpoint
```http
POST /users/chats/upload/presign
```

### Description
Creates an attachment record and returns a short‑lived SAS URL for direct browser upload to storage.

### Request Body
```json
{
  "filename": "report.pdf",
  "mimetype": "application/pdf",
  "size_bytes": 123456
}
```

Validation
- Allowed types are configured via environment `ALLOWED_MIMETYPES`.
- Max size is configured via `MAX_UPLOAD_FILE_SIZE_BYTES` (default: 52428800 ~ 50MB).

### Success Response (200)
```json
{
  "status": "SUCCESS",
  "message": "Presigned upload URL created",
  "data": {
    "attachment_id": "2b5a4d8e-...",
    "filename": "report.pdf",
    "mimetype": "application/pdf",
    "upload_url": "https://.../container/attachments/...sas...",
    "size_bytes": 123456,
    "expires_in_seconds": 900,
    "status": "pending_upload",
    "success": true,
    "error_message": null
  }
}
```

### Error Responses
- 400 Bad Request — invalid/unsupported mimetype or size exceeds limit
- 500 Internal Server Error — failure to create record or generate URL

### Example cURL
```bash
curl -X POST "$BASE_URL/users/chats/upload/presign" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"filename":"report.pdf","mimetype":"application/pdf","size_bytes":123456}'
```

---

## 2) Upload Bytes to SAS URL (browser)

Use the `upload_url` from presign to PUT the bytes directly to storage.

```js
async function uploadToSAS(file, uploadUrl){
  const res = await fetch(uploadUrl, {
    method: 'PUT',
    headers: { 'Content-Type': file.type },
    body: file
  });
  if(!res.ok) throw new Error('Upload failed');
}
```

---

## 3) Finalize Single Upload

### Endpoint
```http
POST /users/chats/upload/finalize
```

### Description
Signals the server that the upload completed; verifies blob and enqueues background processing (checksum, thumbnails when applicable).

### Request Body
```json
{ "attachment_id": "2b5a4d8e-...", "checksum": null }
```

### Success Response (200)
```json
{
  "status": "SUCCESS",
  "message": "Attachment finalized",
  "data": {
    "attachment_id": "2b5a4d8e-...",
    "uploaded_at": "2025-10-06T08:12:00Z",
    "status": "processing"
  }
}
```

### Error Responses
- 404 Not Found — attachment not found or not owned by user
- 400 Bad Request — invalid state (e.g., not in `pending_upload`)
- 500 Internal Server Error — finalize failure

---

## 4) Get Attachment Status

### Endpoint
```http
GET /users/chats/upload/attachment/{attachment_id}
```

### Description
Polls the current status of an attachment.

### Success Response (200)
```json
{
  "status": "SUCCESS",
  "message": "Attachment status retrieved",
  "data": {
    "attachment_id": "2b5a4d8e-...",
    "uploaded_at": "2025-10-06T08:12:00Z",
    "status": "processing"
  }
}
```

### Error Responses
- 404 Not Found — not found or not accessible
- 500 Internal Server Error — retrieval error

---

## 5) Generate Download URL (SAS)

### Endpoint
```http
GET /users/chats/upload/attachment/{attachment_id}/download?expires_minutes=30
```

### Description
Returns a short‑lived read SAS URL for the original attachment. Only available when `status` is `completed`.

### Success Response (200)
```json
{
  "status": "SUCCESS",
  "message": "Download URL generated",
  "data": {
    "attachment_id": "2b5a4d8e-...",
    "download_url": "https://...sas...",
    "expires_in_seconds": 1800
  }
}
```

### Error Responses
- 400 Bad Request — "Attachment is not ready" (not `completed`)
- 404 Not Found — not found or not accessible
- 500 Internal Server Error — URL generation failure

---

## 6) Stream Attachment Status (SSE)

### Endpoint
```http
GET /users/chats/upload/attachments/stream?attachment_ids=<UUID>&attachment_ids=<UUID>
```

### Description
Streams real‑time status updates for specific attachments (if `attachment_ids` provided) or all non‑terminal attachments owned by the user.

### Event Types
- `status` — array of attachments with fields: `attachment_id`, `status`, `updated_at`, `error_message`, `thumbnail_small_url` (when available)
- `ping` — heartbeat
- `complete` — all watched attachments reached `completed` or `failed`
- `timeout` — stream timeout; reconnect allowed
- `error` — internal stream error; reconnect allowed

### Minimal Browser Example
```js
const params = new URLSearchParams();
attachmentIds.forEach(id => params.append('attachment_ids', id));
const es = new EventSource(`/users/chats/upload/attachments/stream?${params.toString()}`);
es.addEventListener('status', (e) => {
  const list = JSON.parse(e.data); // array
  // update per-attachment UI
});
es.addEventListener('complete', () => es.close());
```

---

## 7) Presign Batch

### Endpoint
```http
POST /users/chats/upload/presign/batch
```

### Request Body
```json
{
  "files": [
    { "filename": "a.pdf", "mimetype": "application/pdf", "size_bytes": 1000 },
    { "filename": "b.jsonl", "mimetype": "application/jsonl", "size_bytes": 2000 }
  ]
}
```

### Success Response (200)
```json
{
  "status": "SUCCESS",
  "message": "Batch presign completed",
  "data": {
    "files": [
      {
        "attachment_id": "...",
        "filename": "a.pdf",
        "mimetype": "application/pdf",
        "upload_url": "https://...sas...",
        "size_bytes": 1000,
        "success": true,
        "error_message": null,
        "expires_in_seconds": 900,
        "status": "pending_upload"
      },
      {
        "attachment_id": null,
        "filename": "b.jsonl",
        "mimetype": null,
        "upload_url": null,
        "size_bytes": null,
        "success": false,
        "error_message": "MIME type 'application/jsonl' is not allowed.",
        "expires_in_seconds": 0,
        "status": "failed"
      }
    ],
    "errors": []
  }
}
```

---

## 8) Finalize Batch

### Endpoint
```http
POST /users/chats/upload/finalize/batch
```

### Request Body
```json
{ "files": [ { "attachment_id": "...", "checksum": null } ] }
```

### Success Response (200)
```json
{
  "status": "SUCCESS",
  "message": "Batch finalize completed",
  "data": {
    "results": [
      {
        "attachment_id": "...",
        "status": "processing",
        "error": null,
        "updated_at": "2025-10-06T08:12:10Z"
      }
    ],
    "errors": []
  }
}
```

### Error Responses
- 500 Internal Server Error — "All finalize operations failed" (if none succeeded)

---

## Configuration & Limits
- Allowed MIME types (default): `application/pdf`, `text/plain`, `text/csv`, `text/tab-separated-values`, `text/tsv`, `text/markdown`, `text/x-markdown`, `application/x-ndjson`, `application/ndjson`, `application/jsonl`, `application/json`.
- Max file size (default): `52428800` bytes (~50MB).
- Environment variables:
  - `ALLOWED_MIMETYPES`
  - `MAX_UPLOAD_FILE_SIZE_BYTES`

---

## Suggested Frontend Flow
1) Call `POST /presign` (or `/presign/batch`) for selected file(s).
2) `PUT` file bytes to each `upload_url` (direct to storage).
3) Call `POST /finalize` (or `/finalize/batch`).
4) Open SSE `/attachments/stream?attachment_ids=...` to observe progress until `complete`.
5) Once `completed`, include the `attachment_id` in chat message payloads and/or fetch a download URL when user clicks download.
6) Message lists (`/users/chats/messages/list`) return thumbnail URLs for rendering previews.
