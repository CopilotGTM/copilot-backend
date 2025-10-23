# Knowledge Base API Documentation

## Base URL
```
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

---

## 1. Batch Presign Upload URLs

### Endpoint
```http
POST /users/knowledge/presign/batch
```

### Description
Registers one or more files and returns SAS upload URLs so the browser can upload directly to storage. Partial success is supported.

### Authentication
Required

### Request Body
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

### Success Response (200)
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

### Endpoint
```http
POST /users/knowledge/finalize/batch
```

### Description
Marks uploaded files as ready for processing and enqueues extraction jobs. Returns per-file success and error details.

### Authentication
Required

### Request Body
```json
{
  "files": [
    { "source_id": "d7b2f0cd-4c9c-44a8-9f2a-998af8db6c34", "checksum": "abc123" }
  ]
}
```

### Success Response (200)
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

### Endpoint
```http
GET /users/knowledge/source/{source_id}
```

### Description
Returns the latest status, metadata, and optional read SAS for a single knowledge source.

### Success Response (200)
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

### Error Responses
- `404` when the source does not exist or access is denied
- `500` for unexpected errors

---

## 4. Stream Source Updates (SSE)

### Endpoint
```http
GET /users/knowledge/sources/stream?source_ids=<uuid>&source_ids=<uuid>
```

### Description
Streams realtime status updates for one or more sources using Server-Sent Events.

### Query Parameters
| Parameter    | Type      | Required | Description                                           |
|--------------|-----------|----------|-------------------------------------------------------|
| `source_ids` | array<UUID> | No       | Restrict stream to specific sources (repeat param)    |

### Event Payload Example
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

### Notes
- Heartbeats every ~25s (event `ping`).
- Stream timeout after ~15 minutes of inactivity.
- Use `Last-Event-ID` header to resume after transient disconnects.

---

## 5. List Sources (Paginated)

### Endpoint
```http
GET /users/knowledge/sources?limit=20&cursor=<opaque>&statuses=completed&source_types=file&q=report&include_urls=true
```

### Description
Returns a cursor-paginated list of knowledge sources with optional filters.

### Query Parameters
| Parameter      | Type           | Required | Description                                                       |
|----------------|----------------|----------|-------------------------------------------------------------------|
| `limit`        | integer        | No       | Items per page (1..100, default 20)                               |
| `cursor`       | string         | No       | Opaque cursor from previous response                              |
| `statuses`     | array<string>  | No       | Filter by status (repeat parameter)                               |
| `source_types` | array<string>  | No       | Filter by type (`file`, `integration`)                            |
| `q`            | string         | No       | Case-insensitive substring match on filename                      |
| `include_urls` | boolean        | No       | Include signed URLs in payload (default `false`)                  |

### Success Response (200)
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

### Error Responses
- `400` for invalid filters or cursor
- `500` for unexpected errors

---

## 6. Generate Download URL

### Endpoint
```http
GET /users/knowledge/source/{source_id}/download?expires_minutes=120
```

### Description
Generates a temporary read SAS URL for the original file.

### Query Parameters
| Parameter        | Type    | Required | Description                              |
|------------------|---------|----------|------------------------------------------|
| `expires_minutes`| integer | No       | 1..720, default 120 minutes               |

### Success Response (200)
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

### Error Responses
- `404` if source missing or access denied
- `500` unexpected

---

## 7. Retrieve Aggregated Stats

### Endpoint
```http
GET /users/knowledge/sources/stats
```

### Description
Returns aggregate counts for dashboard widgets.

### Success Response (200)
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

## 8. Request Deletion

### Endpoint
```http
DELETE /users/knowledge/source/{source_id}
```

### Description
Soft-deletes a knowledge source and enqueues background cleanup. Returns `202 Accepted` when queued.

### Success Response (202)
```json
{
  "status": "SUCCESS",
  "message": "Deletion enqueued",
  "data": {
    "source_id": "d7b2f0cd-4c9c-44a8-9f2a-998af8db6c34",
    "status": "deleting"
  }
}
```

### Idempotent Response
If already deleted, returns `SUCCESS` with `status: "deleted"`.

### Error Responses
- `404` when source missing or not owned by caller
- `500` when background task could not be enqueued

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
