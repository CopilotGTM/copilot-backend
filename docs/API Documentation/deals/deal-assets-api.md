# Deal Assets API

This API manages file and link assets associated with CRM deals. Assets can be uploaded files (stored in Azure Blob Storage) or external links (e.g. Google Drive, SharePoint). File uploads use a two-step presign/finalize flow where the client uploads directly to blob storage.

**Base URL**: `/users/crm/deals`

---

## Overview

### What are Deal Assets?

| Type | Description | Example |
| --- | --- | --- |
| **File upload** | Files uploaded via the presign/finalize flow | Proposal PDF, contract DOCX, demo recording |
| **External link** | URLs pointing to resources hosted elsewhere | Google Drive folder, SharePoint document, Figma board |

### Asset Directions

| Value | Meaning |
| --- | --- |
| `received` | Received from the prospect/customer |
| `shared` | Sent to the prospect/customer |
| `internal` | Internal-only, never shared externally |

### Suggested Categories

Free-text field, but the following values are recommended: `rfp`, `proposal`, `contract`, `security`, `demo`, `presentation`, `meeting_recording`, `internal`, `other`.

### Asset Lifecycle (Files)

| Status | Description |
| --- | --- |
| `pending_upload` | Presign issued; waiting for client to upload and finalize |
| `active` | Upload finalized or link created; asset is live |
| `failed` | Upload failed or was abandoned |

### Source Types

| Value | Description |
| --- | --- |
| `manual_upload` | User uploaded the file via this API |
| `email_attachment` | Extracted from a synced email |
| `meeting` | Extracted from a meeting recording |
| `crm` | Synced from CRM system |
| `ai_extracted` | Identified and attached by an AI pipeline |

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

export type AssetDirection = "received" | "shared" | "internal";
export type AssetStatus = "pending_upload" | "active" | "failed";
export type AssetSourceType =
  | "manual_upload"
  | "email_attachment"
  | "meeting"
  | "crm"
  | "ai_extracted";
export type LinkStatus = "ok" | "active" | "broken" | "unknown";

export interface DealAssetItem {
  asset_id: UUID;
  deal_id: UUID;
  name: string;
  category: string | null;
  direction: AssetDirection;
  status: AssetStatus;
  filename: string | null;
  mimetype: string | null;
  size_bytes: number | null;
  download_url: string | null;         // time-limited SAS URL (files only)
  thumbnail_small_url: string | null;  // time-limited SAS URL for small thumbnail
  thumbnail_medium_url: string | null; // time-limited SAS URL for medium thumbnail
  external_url: string | null;         // links only
  external_source: string | null;      // e.g. "google_drive", "sharepoint"
  link_status: LinkStatus | null;
  link_checked_at: string | null;      // ISO8601
  kb_source_id: UUID | null;
  source_type: AssetSourceType;
  source_context: string | null;
  deal_stage_when_added: string | null;
  added_by_user_id: UUID;
  created_at: string;                  // ISO8601
  updated_at: string;                  // ISO8601
}

export interface DealAssetsPage {
  items: DealAssetItem[];
  total_count: number;
}

export interface DealAssetPresignResponse {
  asset_id: UUID;
  filename: string;
  upload_url: string;           // SAS URL for direct PUT upload
  expires_in_seconds: number;
  status: "pending_upload";
}

export interface PresignRequest {
  filename: string;             // 1-255 chars
  mimetype: string;             // MIME type, max 255 chars
  size_bytes: number;           // > 0
  name?: string | null;         // display name, max 500 chars (defaults to filename)
  category?: string | null;     // max 100 chars
  direction?: AssetDirection;   // default: "internal"
}

export interface FinalizeRequest {
  asset_id: UUID;
  checksum?: string | null;     // optional MD5/SHA-256, max 128 chars
}

export interface LinkCreateRequest {
  name: string;                 // 1-500 chars
  external_url: string;         // 1-2048 chars
  external_source?: string | null; // max 100 chars
  category?: string | null;     // max 100 chars
  direction?: AssetDirection;   // default: "internal"
}

export interface AssetUpdateRequest {
  name?: string | null;
  category?: string | null;
  direction?: AssetDirection | null;
  external_url?: string | null;
  external_source?: string | null;
  link_status?: LinkStatus | null;
}
```

---

## File Upload Flow

File uploads use a two-step presign/finalize pattern so the client uploads directly to Azure Blob Storage, keeping large files off the API server.

```
Client                          API                       Azure Blob Storage
  |                              |                              |
  |  1. POST presign             |                              |
  |  (filename, mimetype, size)  |                              |
  |----------------------------->|                              |
  |                              |  (creates pending_upload     |
  |                              |   record + SAS URL)          |
  |  { asset_id, upload_url }   |                              |
  |<-----------------------------|                              |
  |                              |                              |
  |  2. PUT file to upload_url   |                              |
  |------------------------------------------------------------->|
  |                              |                              |
  |  3. POST finalize            |                              |
  |  (asset_id)                  |                              |
  |----------------------------->|                              |
  |                              |  (status -> active)          |
  |  { DealAssetItem }          |                              |
  |<-----------------------------|                              |
```

1. **Presign** -- `POST /{deal_id}/assets/upload/presign` returns an `upload_url` (Azure Blob SAS with write permission) and `expires_in_seconds`.
2. **Upload** -- Client sends a `PUT` request with the file body directly to `upload_url`. Set `Content-Type` to the declared mimetype.
3. **Finalize** -- `POST /{deal_id}/assets/upload/finalize` transitions the asset from `pending_upload` to `active`. An optional `checksum` can be provided for integrity verification.

---

## 1. List Deal Assets

Retrieves all active (non-deleted) assets for a deal, ordered by newest first. File assets include time-limited SAS download and thumbnail URLs.

### Endpoint (List Deal Assets)

```http
GET /{deal_id}/assets
```

### Path Parameters (List Deal Assets)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `deal_id` | UUID | Yes | The unique identifier of the deal |

### Success Response (200) (List Deal Assets)

```json
{
  "status": "SUCCESS",
  "message": "Assets retrieved successfully",
  "data": {
    "items": [
      {
        "asset_id": "c1a2b3c4-d5e6-f7a8-b9c0-d1e2f3a4b5c6",
        "deal_id": "a8a3b1db-4d9a-4f3c-a3f6-2a5c1d8b9e1f",
        "name": "Enterprise Proposal v2.pdf",
        "category": "proposal",
        "direction": "shared",
        "status": "active",
        "filename": "Enterprise_Proposal_v2.pdf",
        "mimetype": "application/pdf",
        "size_bytes": 2458900,
        "download_url": "https://storage.blob.core.windows.net/org-.../assets/...?sv=...&se=...",
        "thumbnail_small_url": "https://storage.blob.core.windows.net/org-.../thumbs/...?sv=...",
        "thumbnail_medium_url": "https://storage.blob.core.windows.net/org-.../thumbs/...?sv=...",
        "external_url": null,
        "external_source": null,
        "link_status": null,
        "link_checked_at": null,
        "kb_source_id": null,
        "source_type": "manual_upload",
        "source_context": null,
        "deal_stage_when_added": "Negotiation",
        "added_by_user_id": "b1a7f9c0-1b2c-4f5a-9c1d-8b7c6a5d4e3f",
        "created_at": "2026-03-07T14:30:00Z",
        "updated_at": "2026-03-07T14:31:00Z"
      },
      {
        "asset_id": "d2b3c4d5-e6f7-a8b9-c0d1-e2f3a4b5c6d7",
        "deal_id": "a8a3b1db-4d9a-4f3c-a3f6-2a5c1d8b9e1f",
        "name": "Technical Architecture Diagram",
        "category": "presentation",
        "direction": "internal",
        "status": "active",
        "filename": null,
        "mimetype": null,
        "size_bytes": null,
        "download_url": null,
        "thumbnail_small_url": null,
        "thumbnail_medium_url": null,
        "external_url": "https://docs.google.com/presentation/d/1abc.../edit",
        "external_source": "google_drive",
        "link_status": "ok",
        "link_checked_at": "2026-03-07T12:00:00Z",
        "kb_source_id": null,
        "source_type": "manual_upload",
        "source_context": null,
        "deal_stage_when_added": "Discovery",
        "added_by_user_id": "b1a7f9c0-1b2c-4f5a-9c1d-8b7c6a5d4e3f",
        "created_at": "2026-03-05T09:15:00Z",
        "updated_at": "2026-03-05T09:15:00Z"
      }
    ],
    "total_count": 2
  }
}
```

### Error Responses (List Deal Assets)

| Code | Description |
|------|-------------|
| `422` | Invalid path parameter (e.g., malformed `deal_id`) |
| `404` | Deal not found or not accessible in this organisation |
| `429` | Rate limit exceeded |
| `500` | Internal server error |

### Usage Example (List Deal Assets)

```ts
const baseUrl = '/users/crm/deals';

const response = await fetch(`${baseUrl}/${dealId}/assets`, {
  headers: { Authorization: `Bearer ${token}` }
});
const { data } = await response.json();
// data.items: DealAssetItem[], data.total_count: number
```

---

## 2. Get Single Asset

Retrieves a single asset by ID, including time-limited SAS download and thumbnail URLs.

### Endpoint (Get Single Asset)

```http
GET /{deal_id}/assets/{asset_id}
```

### Path Parameters (Get Single Asset)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `deal_id` | UUID | Yes | The unique identifier of the deal |
| `asset_id` | UUID | Yes | The unique identifier of the asset |

### Success Response (200) (Get Single Asset)

```json
{
  "status": "SUCCESS",
  "message": "Asset retrieved successfully",
  "data": {
    "asset_id": "c1a2b3c4-d5e6-f7a8-b9c0-d1e2f3a4b5c6",
    "deal_id": "a8a3b1db-4d9a-4f3c-a3f6-2a5c1d8b9e1f",
    "name": "Enterprise Proposal v2.pdf",
    "category": "proposal",
    "direction": "shared",
    "status": "active",
    "filename": "Enterprise_Proposal_v2.pdf",
    "mimetype": "application/pdf",
    "size_bytes": 2458900,
    "download_url": "https://storage.blob.core.windows.net/org-.../assets/...?sv=...&se=...",
    "thumbnail_small_url": "https://storage.blob.core.windows.net/org-.../thumbs/...?sv=...",
    "thumbnail_medium_url": "https://storage.blob.core.windows.net/org-.../thumbs/...?sv=...",
    "external_url": null,
    "external_source": null,
    "link_status": null,
    "link_checked_at": null,
    "kb_source_id": null,
    "source_type": "manual_upload",
    "source_context": null,
    "deal_stage_when_added": "Negotiation",
    "added_by_user_id": "b1a7f9c0-1b2c-4f5a-9c1d-8b7c6a5d4e3f",
    "created_at": "2026-03-07T14:30:00Z",
    "updated_at": "2026-03-07T14:31:00Z"
  }
}
```

### Error Responses (Get Single Asset)

| Code | Description |
|------|-------------|
| `422` | Invalid path parameter |
| `404` | Asset or deal not found |
| `429` | Rate limit exceeded |
| `500` | Internal server error |

---

## 3. Presign File Upload

Creates a pending asset record and returns a time-limited Azure Blob SAS URL for direct file upload. The client must upload the file to this URL, then call the finalize endpoint.

### Endpoint (Presign File Upload)

```http
POST /{deal_id}/assets/upload/presign
```

### Path Parameters (Presign File Upload)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `deal_id` | UUID | Yes | The unique identifier of the deal |

### Request Body (Presign File Upload)

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `filename` | string | Yes | 1-255 chars | Original filename of the asset |
| `mimetype` | string | Yes | Max 255 chars | MIME type of the file |
| `size_bytes` | integer | Yes | > 0 | File size in bytes |
| `name` | string | No | Max 500 chars | Display name (defaults to filename) |
| `category` | string | No | Max 100 chars | Asset category |
| `direction` | string | No | `received` \| `shared` \| `internal` | Default: `internal` |

### Request Example (Presign File Upload)

```json
{
  "filename": "Enterprise_Proposal_v2.pdf",
  "mimetype": "application/pdf",
  "size_bytes": 2458900,
  "name": "Enterprise Proposal v2.pdf",
  "category": "proposal",
  "direction": "shared"
}
```

### Success Response (201) (Presign File Upload)

```json
{
  "status": "SUCCESS",
  "message": "Upload presigned successfully",
  "data": {
    "asset_id": "c1a2b3c4-d5e6-f7a8-b9c0-d1e2f3a4b5c6",
    "filename": "Enterprise_Proposal_v2.pdf",
    "upload_url": "https://storage.blob.core.windows.net/org-.../assets/...?sv=...&sig=...&se=...",
    "expires_in_seconds": 3600,
    "status": "pending_upload"
  }
}
```

### Error Responses (Presign File Upload)

| Code | Description |
|------|-------------|
| `422` | Validation error (missing fields, invalid types, size_bytes <= 0) |
| `404` | Deal not found or not accessible in this organisation |
| `429` | Rate limit exceeded |
| `500` | Internal server error |

### Usage Example (Presign File Upload)

```ts
const baseUrl = '/users/crm/deals';

// Step 1: Get presigned URL
const presignRes = await fetch(`${baseUrl}/${dealId}/assets/upload/presign`, {
  method: 'POST',
  headers: {
    'Authorization': `Bearer ${token}`,
    'Content-Type': 'application/json'
  },
  body: JSON.stringify({
    filename: file.name,
    mimetype: file.type,
    size_bytes: file.size,
    category: 'proposal',
    direction: 'shared'
  })
});
const { data: presign } = await presignRes.json();

// Step 2: Upload file directly to Azure Blob Storage
await fetch(presign.upload_url, {
  method: 'PUT',
  headers: {
    'Content-Type': file.type,
    'x-ms-blob-type': 'BlockBlob'
  },
  body: file
});

// Step 3: Finalize the upload
const finalizeRes = await fetch(`${baseUrl}/${dealId}/assets/upload/finalize`, {
  method: 'POST',
  headers: {
    'Authorization': `Bearer ${token}`,
    'Content-Type': 'application/json'
  },
  body: JSON.stringify({ asset_id: presign.asset_id })
});
const { data: asset } = await finalizeRes.json();
```

---

## 4. Finalize File Upload

Transitions a pending asset from `pending_upload` to `active` after the client has uploaded the file to the presigned URL.

### Endpoint (Finalize File Upload)

```http
POST /{deal_id}/assets/upload/finalize
```

### Path Parameters (Finalize File Upload)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `deal_id` | UUID | Yes | The unique identifier of the deal |

### Request Body (Finalize File Upload)

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `asset_id` | UUID | Yes | Valid UUID | The asset ID returned by the presign endpoint |
| `checksum` | string | No | Max 128 chars | Optional MD5/SHA-256 checksum for integrity verification |

### Request Example (Finalize File Upload)

```json
{
  "asset_id": "c1a2b3c4-d5e6-f7a8-b9c0-d1e2f3a4b5c6",
  "checksum": "d41d8cd98f00b204e9800998ecf8427e"
}
```

### Success Response (200) (Finalize File Upload)

```json
{
  "status": "SUCCESS",
  "message": "Upload finalized successfully",
  "data": {
    "asset_id": "c1a2b3c4-d5e6-f7a8-b9c0-d1e2f3a4b5c6",
    "deal_id": "a8a3b1db-4d9a-4f3c-a3f6-2a5c1d8b9e1f",
    "name": "Enterprise Proposal v2.pdf",
    "category": "proposal",
    "direction": "shared",
    "status": "active",
    "filename": "Enterprise_Proposal_v2.pdf",
    "mimetype": "application/pdf",
    "size_bytes": 2458900,
    "download_url": "https://storage.blob.core.windows.net/org-.../assets/...?sv=...&se=...",
    "thumbnail_small_url": null,
    "thumbnail_medium_url": null,
    "external_url": null,
    "external_source": null,
    "link_status": null,
    "link_checked_at": null,
    "kb_source_id": null,
    "source_type": "manual_upload",
    "source_context": null,
    "deal_stage_when_added": "Negotiation",
    "added_by_user_id": "b1a7f9c0-1b2c-4f5a-9c1d-8b7c6a5d4e3f",
    "created_at": "2026-03-07T14:30:00Z",
    "updated_at": "2026-03-07T14:31:00Z"
  }
}
```

### Error Responses (Finalize File Upload)

| Code | Description |
|------|-------------|
| `422` | Validation error (invalid UUID) |
| `404` | Asset or deal not found |
| `409` | Asset is not in `pending_upload` state (already finalized or failed) |
| `429` | Rate limit exceeded |
| `500` | Internal server error |

---

## 5. Add External Link

Creates an external link asset for a deal. The link is immediately active (no upload flow required).

### Endpoint (Add External Link)

```http
POST /{deal_id}/assets/links
```

### Path Parameters (Add External Link)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `deal_id` | UUID | Yes | The unique identifier of the deal |

### Request Body (Add External Link)

| Field | Type | Required | Constraints | Description |
|-------|------|----------|-------------|-------------|
| `name` | string | Yes | 1-500 chars, non-empty | Display name for the linked asset |
| `external_url` | string | Yes | 1-2048 chars, non-empty | URL of the external resource |
| `external_source` | string | No | Max 100 chars | Source system (e.g. `google_drive`, `sharepoint`) |
| `category` | string | No | Max 100 chars | Asset category |
| `direction` | string | No | `received` \| `shared` \| `internal` | Default: `internal` |

### Request Example (Add External Link)

```json
{
  "name": "Technical Architecture Diagram",
  "external_url": "https://docs.google.com/presentation/d/1abc.../edit",
  "external_source": "google_drive",
  "category": "presentation",
  "direction": "internal"
}
```

### Success Response (201) (Add External Link)

```json
{
  "status": "SUCCESS",
  "message": "Link asset created successfully",
  "data": {
    "asset_id": "d2b3c4d5-e6f7-a8b9-c0d1-e2f3a4b5c6d7",
    "deal_id": "a8a3b1db-4d9a-4f3c-a3f6-2a5c1d8b9e1f",
    "name": "Technical Architecture Diagram",
    "category": "presentation",
    "direction": "internal",
    "status": "active",
    "filename": null,
    "mimetype": null,
    "size_bytes": null,
    "download_url": null,
    "thumbnail_small_url": null,
    "thumbnail_medium_url": null,
    "external_url": "https://docs.google.com/presentation/d/1abc.../edit",
    "external_source": "google_drive",
    "link_status": "unknown",
    "link_checked_at": null,
    "kb_source_id": null,
    "source_type": "manual_upload",
    "source_context": null,
    "deal_stage_when_added": "Discovery",
    "added_by_user_id": "b1a7f9c0-1b2c-4f5a-9c1d-8b7c6a5d4e3f",
    "created_at": "2026-03-07T15:00:00Z",
    "updated_at": "2026-03-07T15:00:00Z"
  }
}
```

### Error Responses (Add External Link)

| Code | Description |
|------|-------------|
| `422` | Validation error (empty name, empty URL, invalid direction) |
| `404` | Deal not found or not accessible in this organisation |
| `429` | Rate limit exceeded |
| `500` | Internal server error |

### Usage Example (Add External Link)

```ts
const baseUrl = '/users/crm/deals';

const response = await fetch(`${baseUrl}/${dealId}/assets/links`, {
  method: 'POST',
  headers: {
    'Authorization': `Bearer ${token}`,
    'Content-Type': 'application/json'
  },
  body: JSON.stringify({
    name: 'Security Questionnaire',
    external_url: 'https://company.sharepoint.com/docs/security-q.xlsx',
    external_source: 'sharepoint',
    category: 'security',
    direction: 'received'
  })
});

const { data: linkAsset } = await response.json();
```

---

## 6. Update Asset

Partially updates an asset's metadata. Only the fields included in the request body are updated; omitted fields remain unchanged.

### Endpoint (Update Asset)

```http
PATCH /{deal_id}/assets/{asset_id}
```

### Path Parameters (Update Asset)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `deal_id` | UUID | Yes | The unique identifier of the deal |
| `asset_id` | UUID | Yes | The unique identifier of the asset |

### Request Body (Update Asset)

All fields are optional. Only include fields you want to change.

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| `name` | string | Max 500 chars | Display name |
| `category` | string | Max 100 chars | Asset category |
| `direction` | string | `received` \| `shared` \| `internal` | Direction |
| `external_url` | string | Max 2048 chars | External URL (links only) |
| `external_source` | string | Max 100 chars | Source system |
| `link_status` | string | `ok` \| `active` \| `broken` \| `unknown` | Link health status |

### Request Example (Update Asset)

```json
{
  "name": "Enterprise Proposal v3 - Final",
  "category": "contract",
  "direction": "shared"
}
```

### Success Response (200) (Update Asset)

```json
{
  "status": "SUCCESS",
  "message": "Asset updated successfully",
  "data": {
    "asset_id": "c1a2b3c4-d5e6-f7a8-b9c0-d1e2f3a4b5c6",
    "deal_id": "a8a3b1db-4d9a-4f3c-a3f6-2a5c1d8b9e1f",
    "name": "Enterprise Proposal v3 - Final",
    "category": "contract",
    "direction": "shared",
    "status": "active",
    "filename": "Enterprise_Proposal_v2.pdf",
    "mimetype": "application/pdf",
    "size_bytes": 2458900,
    "download_url": "https://storage.blob.core.windows.net/org-.../assets/...?sv=...&se=...",
    "thumbnail_small_url": null,
    "thumbnail_medium_url": null,
    "external_url": null,
    "external_source": null,
    "link_status": null,
    "link_checked_at": null,
    "kb_source_id": null,
    "source_type": "manual_upload",
    "source_context": null,
    "deal_stage_when_added": "Negotiation",
    "added_by_user_id": "b1a7f9c0-1b2c-4f5a-9c1d-8b7c6a5d4e3f",
    "created_at": "2026-03-07T14:30:00Z",
    "updated_at": "2026-03-07T16:00:00Z"
  }
}
```

### Error Responses (Update Asset)

| Code | Description |
|------|-------------|
| `400` | No fields to update (empty request body) |
| `422` | Validation error (invalid UUID, invalid direction/link_status) |
| `404` | Asset or deal not found |
| `429` | Rate limit exceeded |
| `500` | Internal server error |

---

## 7. Delete Asset

Soft deletes an asset by setting the `is_deleted` flag. The asset will no longer appear in list responses.

### Endpoint (Delete Asset)

```http
DELETE /{deal_id}/assets/{asset_id}
```

### Path Parameters (Delete Asset)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `deal_id` | UUID | Yes | The unique identifier of the deal |
| `asset_id` | UUID | Yes | The unique identifier of the asset |

### Success Response (200) (Delete Asset)

```json
{
  "status": "SUCCESS",
  "message": "Asset deleted successfully",
  "data": {
    "asset_id": "c1a2b3c4-d5e6-f7a8-b9c0-d1e2f3a4b5c6",
    "deal_id": "a8a3b1db-4d9a-4f3c-a3f6-2a5c1d8b9e1f",
    "name": "Enterprise Proposal v2.pdf",
    "category": "proposal",
    "direction": "shared",
    "status": "active",
    "filename": "Enterprise_Proposal_v2.pdf",
    "mimetype": "application/pdf",
    "size_bytes": 2458900,
    "download_url": null,
    "thumbnail_small_url": null,
    "thumbnail_medium_url": null,
    "external_url": null,
    "external_source": null,
    "link_status": null,
    "link_checked_at": null,
    "kb_source_id": null,
    "source_type": "manual_upload",
    "source_context": null,
    "deal_stage_when_added": "Negotiation",
    "added_by_user_id": "b1a7f9c0-1b2c-4f5a-9c1d-8b7c6a5d4e3f",
    "created_at": "2026-03-07T14:30:00Z",
    "updated_at": "2026-03-07T17:00:00Z"
  }
}
```

### Error Responses (Delete Asset)

| Code | Description |
|------|-------------|
| `422` | Invalid path parameter (e.g., malformed UUID) |
| `404` | Asset or deal not found |
| `429` | Rate limit exceeded |
| `500` | Internal server error |

---

## Implementation Notes

- **Soft delete**: Assets are never physically removed; `is_deleted` is set to `true` and the asset is excluded from list queries
- **SAS URLs**: Download and thumbnail URLs are time-limited Azure Blob SAS tokens; clients should not cache them beyond the expiry window
- **Thumbnails**: Generated for supported image/document types; `null` when not available
- **Organisation isolation**: All queries are scoped to the authenticated user's organisation schema
- **Ordering**: List endpoint returns assets ordered by `created_at` descending (newest first)
