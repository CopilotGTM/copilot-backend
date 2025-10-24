Knowledge Base: Upload, Monitor & Access

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
- `pending_upload` → file created, waiting for blob upload.
- `uploaded` → blob exists; ready for finalize.
- `processing` / `extracting` → background jobs running.
- `completed` → text search-ready; thumbnails and filters available.
- `failed` → unrecoverable error; inspect `error_message` for cause.

## Library browsing & management
- **Listing** – `GET /sources` supports filters:
  - `statuses`: `pending_upload`, `uploaded`, `processing`, `extracting`, `completed`, `failed`.
  - `source_types`: `file` (user uploads) or `integration` (ingested connectors).
  - `q`: case-insensitive substring match on filename.
  - `include_urls=true` to inline download + thumbnail SAS links (adds storage calls; use sparingly on large pages).
- **Stats** – `GET /sources/stats` drives dashboard counters (total, by status, by type). Useful for health widgets.
- **Downloads** – `GET /source/{source_id}/download` issues a read SAS with configurable expiry (`expires_minutes` query).
- **Deletion** – `DELETE /source/{source_id}` soft-deletes immediately, enqueues async cleanup, and returns `status="deleting"`. Repeated requests are idempotent (returns `status="deleted"` once complete).

## Real-time update contract (SSE)
- Endpoint emits JSON frames with `event: message` and payload like:
  ```json
  {
    "source_id": "<UUID>",
    "status": "processing",
    "progress": 65,
    "thumbnail_small_url": "https://...",
    "error_message": null
  }
  ```
- Pass `source_ids` query param to scope updates; omit to monitor all non-terminal sources owned by the user.
- `Last-Event-ID` supported for resume. Stream closes automatically when all watched sources reach `completed` or `failed`.

## Permissions & tenancy rules
- All operations require a valid bearer token; server derives `organisation_id` and `user_id` from the JWT.
- Users only see and act on sources they uploaded unless organisation-level policies grant broader access.
- SAS URLs are scoped to a single blob and expire automatically (default 10 minutes for upload, 120 minutes for read/download).

## Error handling patterns
- Batch operations collect per-file `errors[]` so the UI can flag individual failures without retrying the entire batch.
- Domain failures map to HTTP 400 (validation), 403/404 (ownership), or 500 (unexpected). Envelope `status` switches to `error` with a friendly `message`.
- SSE stream keeps running through transient storage/Redis hiccups; consumers should reconnect using backoff on network errors.

## UI guidelines & best practices
- Disable finalize button until each file upload promise resolves; retry presign for individual files when `upload_url` is missing.
- Surface processing progress using SSE updates; show fallback "Processing" chip when no progress percentage is provided.
- Cache `GET /sources` response for short intervals (<5 min) to reduce churn but refresh immediately after finalize or delete.
- Confirm deletions and communicate the action is irreversible; once deletion completes the item disappears from subsequent lists.

## Related documentation
- API details: see `docs/Technical Documentation/knowledge-base.md` for schemas, services, and background workers.
- Integration of upload widgets: reference UI flow diagrams in `docs/API Documentation` for consistency with chat and integrations.

