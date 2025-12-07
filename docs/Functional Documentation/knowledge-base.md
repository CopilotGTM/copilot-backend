# Knowledge Base: Upload, Monitor & Access

Audience: Product managers, business stakeholders, and frontend teams integrating the knowledge base experience.

## What the feature delivers

- Self-serve uploads into the organisation knowledge catalog with per-file progress tracking.
- Real-time visibility into processing (checksums, extraction, thumbnail generation) via Server-Sent Events (SSE).
- Searchable library of previous uploads with optional download links, thumbnail previews, and aggregate stats.
- Safe deletion flow that queues background cleanup without blocking the UI.

## API surface the frontend consumes

All endpoints sit under the `/users/knowledge` base path and return the common `GenericResponse` envelope.

| Capability | Endpoint | Method | Notes |
|------------|----------|--------|-------|
| Presign batch | `/presign/batch` | POST | Returns SAS upload URLs for each file; partial success supported. |
| Finalize batch | `/finalize/batch` | POST | Moves uploaded files into processing, triggers Celery jobs. |
| Source status | `/source/{source_id}` | GET | Returns latest status plus optional download SAS. |
| Live updates | `/sources/stream` | GET (SSE) | Pushes status frames for specified sources or all active ones. |
| List sources | `/sources` | GET | Cursor-based pagination with filters by status, type, and filename. |
| Download link | `/source/{source_id}/download` | GET | Generates short-lived download URL; opt-in expiry. |
| Stats | `/sources/stats` | GET | Aggregated totals by status and type for dashboard use. |
| Delete source | `/source/{source_id}` | DELETE | 202 Accepted; enqueues async deletion and marks soft-deleted. |

## End-to-end upload journey

1. **Request presign links** – Call `POST /presign/batch` with filenames, MIME types, and sizes. The response carries:
   - `files[]` objects with `source_id`, `upload_url`, expiry, and status per file.
   - `errors[]` describing rejected files; accepted files still proceed.
2. **Upload directly to storage** – Browser uploads each file to the provided `upload_url`. The API does not proxy file bytes, keeping uploads fast and cheaper.
3. **Finalize after upload** – Call `POST /finalize/batch` per batch (or per file) once the blob upload completes. The service transitions sources to `processing` and enqueues Celery tasks (`knowledge.post_upload_process`, `knowledge.document_extraction`).
4. **Monitor progress** – Subscribe to `/sources/stream` (SSE) passing the relevant `source_id`s. Frames include `status`, `progress`, `thumbnail_*` URLs, and error messages. Heartbeats are emitted ~25s; the stream auto-ends after 15 minutes of inactivity.
5. **Persisted state** – When processing completes, `GET /source/{source_id}` returns final metadata, including optional read SAS URLs (120 min default) and checksum.

### Status lifecycle (frontend badge mapping)

| Status | Meaning | Terminal? | UI Action |
|--------|---------|-----------|----------|
| `pending_upload` | File created, waiting for blob upload | No | Show "Waiting" badge |
| `uploaded` | Blob exists; ready for finalize | No | Enable "Process" button |
| `processing` | Initial processing (checksum, thumbnails) | No | Show "Preparing..." |
| `extracting` | Text extraction in progress (0-100%) | No | Show progress bar |
| `completed` | Text search-ready; thumbnails available | Yes | Show success, enable actions |
| `failed` | Unrecoverable error; see `error_message` | Yes | Show error with retry option |
| `deleting` | Deletion in progress | Yes* | Show "Deleting..." |
| `deleted` | Permanently removed (SSE event only) | Yes | Remove from UI |

> **Note:** `deleting` is terminal for SSE purposes but source still exists until `deleted` event.

### Extraction progress stages

During `extracting` status, the `progress` field updates through these stages:

| Progress | What's happening |
|----------|------------------|
| 5% | Worker picked up job |
| 20% | Document parsed, pages extracted |
| 20-80% | Processing each chunk (embeddings, analysis) |
| 90% | Storing results to database |
| 100% | Extraction complete |

**Thumbnail availability:** Thumbnail URLs become available at ~20% progress when the first page is processed.

## Library browsing & management

- **Listing** – `GET /sources` supports filters:
  - `statuses`: `pending_upload`, `uploaded`, `processing`, `extracting`, `completed`, `failed`.
  - `source_types`: `file` (user uploads) or `integration` (ingested connectors).
  - `q`: case-insensitive substring match on filename.
  - `updated_after`: ISO8601 timestamp to filter by update time (useful for recent failures).
  - `include_urls=true` to inline download + thumbnail SAS links (adds storage calls; use sparingly on large pages).
  
- **Failed files behavior**:
  - By default, `failed` files are **excluded** from list results to keep normal browsing clean.
  - To fetch failed files, explicitly pass `statuses=failed`.
  - To fetch recent failures only, combine with `updated_after` (e.g., last 7 days).

- **is_active field** – Each list item includes `is_active: bool` indicating if the source needs SSE monitoring. Active statuses: `pending_upload`, `uploaded`, `processing`, `extracting`, `deleting`.

- **Stats** – `GET /sources/stats` drives dashboard counters:
  - `total`, `by_status`, `by_type` (existing)
  - `active_count`: sources needing SSE monitoring
  - `recent_failures_24h`: failed sources in last 24 hours
  
- **Downloads** – `GET /source/{source_id}/download` issues a read SAS with configurable expiry (`expires_minutes` query).
- **Deletion** – `DELETE /sources` soft-deletes immediately, enqueues async cleanup, and returns `status="deleting"`. Use `source_id` (not `job_id`) for SSE tracking.

## Real-time update contract (SSE)

### Connection

- **Endpoint:** `GET /sources/stream?source_ids=<uuid>&source_ids=<uuid>`
- **Heartbeat:** Every 25 seconds (`ping` event)
- **Timeout:** 900 seconds (15 minutes) - reconnect after `timeout` event
- **Scope:** Pass `source_ids` to watch specific sources; omit to watch all non-terminal sources

### Event Types

| Event | When | Action Required |
|-------|------|----------------|
| `status` | Status or progress changed | Update UI state |
| `deleted` | Source permanently removed | Remove from UI |
| `complete` | All sources reached terminal status | Close connection |
| `ping` | Every 25 seconds | None (keep-alive) |
| `timeout` | After 15 minutes | Reconnect |
| `error` | Server error | Reconnect with backoff |

### Status Event Payload

```json
{
  "event": "status",
  "data": [{
    "source_id": "<UUID>",
    "status": "extracting",
    "progress": 45,
    "updated_at": "2025-12-07T10:02:00Z",
    "error_message": null,
    "thumbnail_small_url": "https://...",
    "thumbnail_medium_url": "https://..."
  }]
}
```

### Deleted Event Payload

```json
{
  "event": "deleted",
  "data": [{
    "source_id": "<UUID>",
    "status": "deleted",
    "progress": 100
  }]
}
```

### Frontend Integration Pattern

```javascript
const eventSource = new EventSource(`/api/users/knowledge/sources/stream?source_ids=${id}`);

eventSource.addEventListener('status', (e) => {
  const updates = JSON.parse(e.data);
  updates.forEach(u => {
    updateProgressBar(u.source_id, u.progress);
    if (u.thumbnail_small_url) showThumbnail(u.source_id, u.thumbnail_small_url);
  });
});

eventSource.addEventListener('deleted', (e) => {
  const updates = JSON.parse(e.data);
  updates.forEach(u => removeFromUI(u.source_id));
});

eventSource.addEventListener('complete', () => eventSource.close());
eventSource.addEventListener('timeout', () => reconnectWithBackoff());
eventSource.addEventListener('error', () => reconnectWithBackoff());
```

## Permissions & tenancy rules

- All operations require a valid bearer token; server derives `organisation_id` and `user_id` from the JWT.
- Users only see and act on sources they uploaded unless organisation-level policies grant broader access.
- SAS URLs are scoped to a single blob and expire automatically (default 10 minutes for upload, 120 minutes for read/download).

## Error handling patterns

- Batch operations collect per-file `errors[]` so the UI can flag individual failures without retrying the entire batch.
- Domain failures map to HTTP 400 (validation), 403/404 (ownership), or 500 (unexpected). Envelope `status` switches to `error` with a friendly `message`.
- SSE stream keeps running through transient storage/Redis hiccups; consumers should reconnect using backoff on network errors.

## UI guidelines & best practices

### Upload Flow
- Disable finalize button until each file upload promise resolves
- Retry presign for individual files when `upload_url` is missing
- Connect to SSE immediately after `finalize/batch` returns

### Progress Display
- Show progress bar during `extracting` status (0-100%)
- Display thumbnail preview when `thumbnail_small_url` becomes available (~20% progress)
- Show fallback "Processing" chip when no progress percentage is provided

### SSE Connection Management
- Reconnect with exponential backoff on disconnect: 1s, 2s, 4s, 8s, 16s
- Close connection when receiving `complete` event
- Handle `timeout` event (15 min) by reconnecting if user is still on page
- Reset reconnect counter on successful `ping` event

### State Management
- Cache `GET /sources` response for short intervals (<5 min)
- Refresh immediately after finalize or delete actions
- Merge SSE updates into local state; don't replace entire list
- Remove source from state on `deleted` event (not `deleting`)

### Deletion UX
- Confirm deletions and communicate the action is irreversible
- Show "Deleting..." indicator immediately on API success (optimistic UI)
- Remove item from list only after receiving `deleted` SSE event

### Error Handling
- Display `error_message` field when status is `failed`
- Offer retry option for failed uploads (re-upload flow)
- Show reconnection indicator when SSE connection drops

## Related documentation

- API details: see `docs/Technical Documentation/knowledge-base.md` for schemas, services, and background workers.
- Integration of upload widgets: reference UI flow diagrams in `docs/API Documentation` for consistency with chat and integrations.
