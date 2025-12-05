# Knowledge Base API Documentation

## Base URL

```text
/users/knowledge
```

---

## Response Envelope

All endpoints return the shared `GenericResponse` envelope.

```json
{
  "status": "SUCCESS | ERROR",
  "message": "Human-readable message",
  "data": { ... } | null
}
```

---

## Authentication

- Required: Bearer token (`Authorization: Bearer <access_token>`)
- User identity (organisation, user, role) resolved from the token; clients never pass these explicitly

---

## Rate Limiting (defaults)

- Write endpoints (`POST`, `DELETE`): ~15/min, 100/hour per user
- Read endpoints (`GET`): ~60/min, 1000/hour per user
- On limit breach: `429 Too Many Requests` with `Retry-After`

---

## Status Lifecycle

| Status            | Meaning                                                     | UI Guidance                           |
|-------------------|-------------------------------------------------------------|----------------------------------------|
| `pending_upload`  | File registered, waiting for blob upload                    | Show "Waiting for upload" badge        |
| `uploaded`        | Blob present, ready for finalize                            | Enable "Finalize" action               |
| `processing`      | Background jobs (checksum, thumbnails, extraction) running  | Show progress indicator if available   |
| `extracting`      | Text extraction specifically in progress                    | Display spinner + percentage if known |
| `completed`       | Document ready for search and download                      | Mark as available                      |
| `failed`          | Processing error; see `error_message` for cause             | Show error state with retry guidance   |
| `deleting`        | Deletion in progress (background cleanup running)           | Show "Deleting..." indicator           |

---

## 1. Batch Presign Upload URLs

### Batch Presign Upload URLs Endpoint

```http
POST /users/knowledge/presign/batch
```

### Batch Presign Upload URLs Description

Registers one or more files and returns SAS upload URLs so the browser can upload directly to storage. Partial success is supported.

### Batch Presign Upload URLs Authentication

Required

### Batch Presign Upload URLs Request Body

```json
{
  "files": [
    { "filename": "report.pdf", "mimetype": "application/pdf", "size_bytes": 5242880 }
  ],
  "default_filters": { "department": "sales" },
  "client_batch_id": "batch-123"
}
```

### Request Fields

| Field             | Type    | Required | Description                                      |
|-------------------|---------|----------|--------------------------------------------------|
| `files`           | array   | Yes      | File descriptors; at least one required          |
| `filename`        | string  | Yes      | 1..255 characters                                |
| `mimetype`        | string  | Yes      | Detected MIME type                               |
| `size_bytes`      | integer | Yes      | File size in bytes (> 0)                          |
| `default_filters` | object  | No       | Metadata auto-applied to each source             |
| `client_batch_id` | string  | No       | Client correlation token (<= 100 characters)    |

### Batch Presign Upload URLs Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Batch presign completed",
  "data": {
    "files": [
      {
        "source_id": "d7b2f0cd-4c9c-44a8-9f2a-998af8db6c34",
        "filename": "report.pdf",
        "mimetype": "application/pdf",
        "size_bytes": 5242880,
        "upload_url": "https://...",
        "expires_in_seconds": 600,
        "status": "pending_upload",
        "success": true
      }
    ],
    "errors": []
  }
}
```

### Error Response (200 with partial errors)

```json
{
  "status": "SUCCESS",
  "message": "Batch presign completed with 1 errors",
  "data": {
    "files": [
      {
        "source_id": "00000000-0000-0000-0000-000000000000",
        "filename": "bad.exe",
        "mimetype": "",
        "size_bytes": 0,
        "upload_url": null,
        "expires_in_seconds": 0,
        "status": "failed",
        "error_message": "Unsupported mimetype",
        "success": false
      }
    ],
    "errors": [
      { "filename": "bad.exe", "reason": "Unsupported mimetype" }
    ]
  }
}
```

### Error Response (400)

```json
{
  "status": "ERROR",
  "message": "File list cannot be empty",
  "data": null
}
```

---

## 2. Batch Finalize Uploads

### Batch Finalize Uploads Endpoint

```http
POST /users/knowledge/finalize/batch
```

### Batch Finalize Uploads Description

Marks uploaded files as ready for processing and enqueues extraction jobs. Returns per-file success and error details.

### Batch Finalize Uploads Authentication

Required

### Request Body

```json
{
  "files": [
    { "source_id": "d7b2f0cd-4c9c-44a8-9f2a-998af8db6c34", "checksum": "abc123" }
  ]
}
```

### Batch Finalize Uploads Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Batch finalize completed",
  "data": {
    "results": [
      {
        "source_id": "d7b2f0cd-4c9c-44a8-9f2a-998af8db6c34",
        "status": "processing",
        "error": null
      }
    ],
    "errors": []
  }
}
```

### Partial Error Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Batch finalize completed",
  "data": {
    "results": [],
    "errors": [
      { "source_id": "7c153f76-1bf3-4b5b-9f96-1af9a5bf8a70", "reason": "missing_upload" }
    ]
  }
}
```

### Error Response (500)

Returned when all finalize operations fail.

---

## 3. Get Source Status

### Get Source Status Endpoint

```http
GET /users/knowledge/source/{source_id}
```

### Get Source Status Description

Returns the latest status, metadata, and optional read SAS for a single knowledge source.

### Get Source Status Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Source status retrieved",
  "data": {
    "source_id": "d7b2f0cd-4c9c-44a8-9f2a-998af8db6c34",
    "filename": "report.pdf",
    "mimetype": "application/pdf",
    "size_bytes": 5242880,
    "status": "completed",
    "created_at": "2025-10-10T10:00:00Z",
    "updated_at": "2025-10-10T10:05:00Z",
    "error_message": null,
    "storage_path": "org/123/report.pdf",
    "read_url": "https://...",
    "filters": { "department": "sales" },
    "checksum": "abc123",
    "thumbnail_small_path": "thumbnails/small.png",
    "thumbnail_medium_path": "thumbnails/medium.png"
  }
}
```

### Get Source Status Error Responses

- `404` when the source does not exist or access is denied
- `500` for unexpected errors

---

## 4. Stream Source Updates (SSE)

### Stream Source Updates Endpoint

```http
GET /users/knowledge/sources/stream?source_ids=<uuid>&source_ids=<uuid>
```

### Stream Source Updates Description

Streams realtime status updates for one or more sources using Server-Sent Events.

### Stream Source Updates Query Parameters

| Parameter    | Type      | Required | Description                                           |
|--------------|-----------|----------|-------------------------------------------------------|
| `source_ids` | array[UUID] | No       | Restrict stream to specific sources (repeat param)    |

### Stream Source Updates Event Payload Example

```json
{
  "source_id": "d7b2f0cd-4c9c-44a8-9f2a-998af8db6c34",
  "status": "processing",
  "updated_at": "2025-10-10T10:02:00Z",
  "progress": 65,
  "error_message": null,
  "thumbnail_small_url": null,
  "thumbnail_medium_url": null
}
```

### Stream Source Updates Notes

- Heartbeats every ~25s (event `ping`).
- Stream timeout after ~15 minutes of inactivity.
- Use `Last-Event-ID` header to resume after transient disconnects.

---

## 5. List Sources (Paginated)

### List Sources Endpoint

```http
GET /users/knowledge/sources?limit=20&cursor=<opaque>&statuses=completed&source_types=file&q=report&include_urls=true
```

### List Sources Description

Returns a cursor-paginated list of knowledge sources with optional filters.

### List Sources Query Parameters

| Parameter      | Type           | Required | Description                                                       |
|----------------|----------------|----------|-------------------------------------------------------------------|
| `limit`        | integer        | No       | Items per page (1..100, default 20)                               |
| `cursor`       | string         | No       | Opaque cursor from previous response                              |
| `statuses`     | array[string]  | No       | Filter by status (repeat parameter)                               |
| `source_types` | array[string]  | No       | Filter by type (`file`, `integration`)                            |
| `q`            | string         | No       | Case-insensitive substring match on filename                      |
| `include_urls` | boolean        | No       | Include signed URLs in payload (default `false`)                  |

### List Sources Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Sources listed",
  "data": {
    "items": [
      {
        "source_id": "d7b2f0cd-4c9c-44a8-9f2a-998af8db6c34",
        "filename": "report.pdf",
        "mimetype": "application/pdf",
        "size_bytes": 5242880,
        "status": "completed",
        "created_at": "2025-10-10T10:00:00Z",
        "updated_at": "2025-10-10T10:05:00Z",
        "error_message": null,
        "checksum": "abc123",
        "thumbnail_small_url": null,
        "thumbnail_medium_url": null
      }
    ],
    "cursor": null
  }
}
```

### List Sources Error Responses

- `400` for invalid filters or cursor
- `500` for unexpected errors

---

## 6. Generate Download URL

### Generate Download URL Endpoint

```http
GET /users/knowledge/source/{source_id}/download?expires_minutes=120
```

### Generate Download URL Description

Generates a temporary read SAS URL for the original file.

### Generate Download URL Query Parameters

| Parameter        | Type    | Required | Description                              |
|------------------|---------|----------|------------------------------------------|
| `expires_minutes`| integer | No       | 1..720, default 120 minutes               |

### Generate Download URL Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Download URL generated",
  "data": {
    "source_id": "d7b2f0cd-4c9c-44a8-9f2a-998af8db6c34",
    "download_url": "https://...",
    "expires_in_seconds": 7200
  }
}
```

### Generate Download URL Error Responses

- `404` if source missing or access denied
- `500` unexpected

---

## 7. Retrieve Aggregated Stats

### Retrieve Aggregated Stats Endpoint

```http
GET /users/knowledge/sources/stats
```

### Retrieve Aggregated Stats Description

Returns aggregate counts for dashboard widgets.

### Retrieve Aggregated Stats Success Response (200)

```json
{
  "status": "SUCCESS",
  "message": "Source stats retrieved",
  "data": {
    "total": 42,
    "by_status": {
      "processing": 5,
      "completed": 30,
      "failed": 7
    },
    "by_type": {
      "file": 33,
      "integration": 9
    }
  }
}
```

---

## 8. Delete Sources

### Delete Sources Endpoint

```http
DELETE /users/knowledge/sources
```

### Delete Sources Description

Delete one or more knowledge sources in a single request. This unified endpoint handles both single file and batch deletion - simply pass one or more source IDs. The endpoint:

1. **Validates ownership** - Each source must belong to the authenticated user's organisation
2. **Creates deletion jobs** - A `KnowledgeProcessingJob` record is created for each source to track deletion progress
3. **Updates source status** - Source status changes to `deleting` and `is_deleted` flag is set
4. **Enqueues background tasks** - Celery tasks are dispatched to perform actual cleanup (blob deletion, document removal)
5. **Returns immediately** - Returns `202 Accepted` with job tracking information

Supports partial success for batch operations - some sources may be deleted while others fail validation.

### Delete Sources Request Body

```json
{
  "source_ids": [
    "d7b2f0cd-4c9c-44a8-9f2a-998af8db6c34"
  ],
  "client_batch_id": "optional-correlation-id"
}
```

For batch deletion:

```json
{
  "source_ids": [
    "d7b2f0cd-4c9c-44a8-9f2a-998af8db6c34",
    "e8c3f1de-5d0a-55b9-0f3b-009bf9ec7d45",
    "f9d4g2ef-6e1b-66ca-1g4c-110cg0fd8e56"
  ],
  "client_batch_id": "optional-correlation-id"
}
```

### Delete Sources Request Fields

| Field             | Type         | Required | Description                                      |
|-------------------|--------------|----------|--------------------------------------------------|
| `source_ids`      | array[UUID]  | Yes      | List of source IDs to delete (1-100 items)       |
| `client_batch_id` | string       | No       | Client correlation token (<= 100 characters)     |

### Delete Sources Success Response (202)

Single source:

```json
{
  "status": "SUCCESS",
  "message": "Deletion enqueued",
  "data": {
    "batch_id": "b1234567-89ab-cdef-0123-456789abcdef",
    "results": [
      {
        "source_id": "d7b2f0cd-4c9c-44a8-9f2a-998af8db6c34",
        "status": "deleting",
        "job_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
      }
    ],
    "errors": []
  }
}
```

Multiple sources:

```json
{
  "status": "SUCCESS",
  "message": "Batch deletion enqueued (3 sources)",
  "data": {
    "batch_id": "b1234567-89ab-cdef-0123-456789abcdef",
    "results": [
      {
        "source_id": "d7b2f0cd-4c9c-44a8-9f2a-998af8db6c34",
        "status": "deleting",
        "job_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
      },
      {
        "source_id": "e8c3f1de-5d0a-55b9-0f3b-009bf9ec7d45",
        "status": "already_deleted",
        "job_id": null
      }
    ],
    "errors": []
  }
}
```

### Delete Sources Response Fields

| Field                  | Type        | Description                                              |
|------------------------|-------------|----------------------------------------------------------|
| `batch_id`             | UUID        | Server-generated ID for this deletion batch              |
| `results`              | array       | Successfully processed sources                           |
| `results[].source_id`  | UUID        | The source ID                                            |
| `results[].status`     | string      | `"deleting"` or `"already_deleted"`                      |
| `results[].job_id`     | UUID\|null  | Job tracking ID (null if already deleted)                |
| `errors`               | array       | Sources that failed validation                           |
| `errors[].source_id`   | UUID        | The source ID that failed                                |
| `errors[].reason`      | string      | Error code: `"not_found"`, `"already_deleted"`, `"db_error"` |
| `errors[].message`     | string      | Human-readable error description                         |

### Delete Sources Partial Error Response (202)

```json
{
  "status": "SUCCESS",
  "message": "Batch deletion enqueued (2 sources) with 1 errors",
  "data": {
    "batch_id": "b1234567-89ab-cdef-0123-456789abcdef",
    "results": [
      {
        "source_id": "d7b2f0cd-4c9c-44a8-9f2a-998af8db6c34",
        "status": "deleting",
        "job_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
      }
    ],
    "errors": [
      {
        "source_id": "f9d4g2ef-6e1b-66ca-1g4c-110cg0fd8e56",
        "reason": "not_found",
        "message": "Source not found or access denied"
      }
    ]
  }
}
```

### Delete Sources Idempotent Behavior

- If a source is already deleted, it appears in `errors` with `reason: "already_deleted"`
- If a source is currently being deleted (status = `deleting`), it appears in `results` with the existing `job_id`
- Repeated deletion requests for the same source are safe and will not cause errors

### Delete Sources Error Responses

- `400` when request validation fails (empty source_ids, exceeds 100 limit)
- `400` when all deletion requests failed (all sources not found or already deleted)
- `500` unexpected server error

### Delete Sources Background Processing

After the API returns `202 Accepted`, background workers perform the actual cleanup:

1. **Delete derived documents** - Removes all chunks from `knowledge_base` table
2. **Delete associated jobs** - Cleans up `KnowledgeProcessingJob` records
3. **Delete blobs** - Removes original file and thumbnails from Azure storage
4. **Hard-delete source** - Permanently removes the `OrganisationSource` record

Progress is published via Redis pub/sub and can be monitored through SSE (see below).

- `500` unexpected server error

---

## 9. Deletion Status via SSE

To track deletion progress in real-time, subscribe to the SSE stream endpoint with the source IDs being deleted.

### SSE Stream Endpoint for Deletion

```http
GET /users/knowledge/sources/stream?source_ids=<uuid>&source_ids=<uuid>
```

Pass the `source_id` values from the deletion response to track specific sources. When subscribed to `/sources/stream`, deletion progress is streamed as follows:

### Deletion Progress Event

During deletion, the worker publishes progress updates:

| Progress | Stage Description                        |
|----------|------------------------------------------|
| 0%       | Deletion job started                     |
| 100%     | Deletion complete, source removed        |

```json
{
  "event": "status",
  "data": [{
    "source_id": "d7b2f0cd-4c9c-44a8-9f2a-998af8db6c34",
    "status": "deleting",
    "progress": 50,
    "error_message": null
  }]
}
```

### Deletion Completed Event

When deletion completes successfully, a special `deleted` event type is emitted to signal the UI should remove the source from its state:

```json
{
  "event": "deleted",
  "data": [{
    "source_id": "d7b2f0cd-4c9c-44a8-9f2a-998af8db6c34",
    "status": "deleted",
    "progress": 100
  }]
}
```

### Deletion Failed Event

If deletion fails at any stage, the source status changes to `failed` with an error message:

```json
{
  "event": "status",
  "data": [{
    "source_id": "d7b2f0cd-4c9c-44a8-9f2a-998af8db6c34",
    "status": "failed",
    "progress": 60,
    "error_message": "Blob deletion failed: Azure storage timeout"
  }]
}
```

### Frontend Integration Example

```javascript
// After calling DELETE /users/knowledge/sources
const response = await deleteSourcesAPI({ source_ids: [...] });
const sourceIds = response.data.results.map(r => r.source_id);

// Subscribe to SSE for deletion progress
const params = sourceIds.map(id => `source_ids=${id}`).join('&');
const eventSource = new EventSource(`/users/knowledge/sources/stream?${params}`);

eventSource.addEventListener('status', (event) => {
  const updates = JSON.parse(event.data);
  updates.forEach(update => {
    if (update.status === 'deleting') {
      updateProgressUI(update.source_id, update.progress);
    } else if (update.status === 'failed') {
      showErrorUI(update.source_id, update.error_message);
    }
  });
});

eventSource.addEventListener('deleted', (event) => {
  const updates = JSON.parse(event.data);
  updates.forEach(update => {
    removeSourceFromUI(update.source_id);
  });
});
```

---

## Error Codes Summary

| HTTP Code | Reason                                              | Suggested Action                         |
|-----------|------------------------------------------------------|------------------------------------------|
| 400       | Validation failure (missing fields, invalid cursor)  | Fix request payload                      |
| 401       | Missing/invalid token                                | Ensure valid bearer token                |
| 403       | Access denied (ownership)                            | Confirm user permissions                 |
| 404       | Source not found                                     | Verify `source_id`                       |
| 409/422   | Conflicting state (rare)                             | Retry after resolving conflict           |
| 429       | Rate limit exceeded                                  | Respect `Retry-After` header             |
| 500       | Unexpected server error                              | Retry with backoff; contact support      |

---

## Frontend Integration Notes

1. Presign -> upload -> finalize sequence is required per file.
2. Use SSE stream for real-time status; reconnect with backoff on network errors.
3. Cache list responses briefly, but refresh after finalize or delete actions.
4. Always confirm deletions with the user; action is irreversible once cleanup completes.

---

## Related References

- Functional overview: `docs/Functional Documentation/knowledge-base.md`
- Technical implementation: `docs/Technical Documentation/knowledge-base.md`
