# Deal Memory API

This API retrieves the persistent AI-generated memory document for a CRM deal. The memory is a markdown document that accumulates knowledge from meetings, emails, notes, and stakeholder interactions over the deal lifecycle.

**Base URL**: `/users/crm/deals`

---

## Overview

### What is Deal Memory?

Deal memory is a persistent markdown document maintained per deal by an AI agent. It synthesizes data from all deal touchpoints into a structured knowledge base that other agents consume for context.

| Aspect | Description |
| --- | --- |
| **Format** | Structured markdown with 12 sections (stakeholders, timeline, risks, etc.) |
| **Generation** | Built automatically via daily backfill or triggered by new meetings/emails |
| **Updates** | Incremental — new data is merged into the existing document |
| **Versioning** | Each update increments the `version` field |

### When is Memory Available?

Memory is generated when a deal has at least one data source (meeting, email, or note). Deals without any data will not have a memory document — the API returns `null` in that case.

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

On limit breach: `429 Too Many Requests` with `Retry-After` header.

---

## Data Shapes (TypeScript)

```ts
export type UUID = string;

export interface DealMemoryResponse {
  memory_id: UUID;
  deal_id: UUID;
  content: string;          // Markdown memory document
  version: number;          // Increments on each update
  token_count: number | null; // Approximate token count
  created_at: string;       // ISO8601
  updated_at: string;       // ISO8601
}
```

---

## 1. Get Deal Memory

Retrieves the persistent memory document for a specific deal. Returns `null` data if no memory has been built yet.

### Endpoint (Get Deal Memory)

```http
GET /{deal_id}/memory
```

### Path Parameters (Get Deal Memory)

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `deal_id` | UUID | Yes | The unique identifier of the deal |

### Success Response — Memory Exists (200)

```json
{
  "status": "SUCCESS",
  "message": "Deal memory retrieved successfully",
  "data": {
    "memory_id": "c4a2e1f0-8b3d-4f6a-9e2c-1d5b7a8c0e3f",
    "deal_id": "16b1327e-09b2-4e22-beb8-6dd4b235f70b",
    "content": "# Deal Memory: Acme Corp Enterprise License\n\n## 1. Stakeholder Map\n| Name | Role | Stance | Last Contact |\n|------|------|--------|-------------|\n| Jane Doe | VP Engineering | Champion | 2026-02-28 |\n...",
    "version": 3,
    "token_count": 4200,
    "created_at": "2026-02-15T08:00:00Z",
    "updated_at": "2026-03-01T10:30:00Z"
  }
}
```

### Success Response — No Memory Yet (200)

```json
{
  "status": "SUCCESS",
  "message": "No memory exists for this deal yet",
  "data": null
}
```

### Response Fields (Get Deal Memory)

| Field | Type | Description |
|-------|------|-------------|
| `memory_id` | UUID | Unique identifier for the memory record |
| `deal_id` | UUID | Deal this memory belongs to |
| `content` | string | Full markdown memory document |
| `version` | integer | Document version, starts at 1, increments on each update |
| `token_count` | integer or null | Approximate token count of the content |
| `created_at` | string (ISO8601) | When the memory was first generated |
| `updated_at` | string (ISO8601) | When the memory was last updated |

### Error Responses (Get Deal Memory)

| Code | Description |
|------|-------------|
| `422` | Invalid path parameter (e.g., malformed `deal_id`) |
| `429` | Rate limit exceeded |
| `500` | Internal server error |

### Usage Example (Get Deal Memory)

```ts
const baseUrl = '/users/crm/deals';

const response = await fetch(`${baseUrl}/${dealId}/memory`, {
  headers: { Authorization: `Bearer ${token}` }
});
const { status, data } = await response.json();

if (data) {
  // Render markdown content in the Deal Memory tab
  renderMarkdown(data.content);
} else {
  // Show empty state: "No memory generated yet"
  showEmptyState();
}
```

---

## Frontend Integration Guide

### Displaying Deal Memory

```tsx
function DealMemoryTab({ dealId }: { dealId: string }) {
  const { data, isLoading } = useDealMemory(dealId);

  if (isLoading) return <Skeleton />;

  if (!data) {
    return (
      <EmptyState
        title="No deal memory yet"
        description="Memory will be generated once the deal has meetings, emails, or notes."
      />
    );
  }

  return (
    <div className="deal-memory">
      <div className="memory-header">
        <span className="text-muted">
          v{data.version} · Updated {formatRelative(data.updated_at)}
        </span>
      </div>
      <MarkdownRenderer content={data.content} />
    </div>
  );
}
```

---

## Implementation Notes

- **Read-only API**: Memory documents are generated by the backend AI agent pipeline, not by user input. This API only serves the read side.
- **Null response is normal**: A `null` data field with `200 SUCCESS` is expected for deals that haven't had memory built yet. The frontend should handle this as an empty state, not an error.
- **Organisation isolation**: The query always filters by the authenticated user's `organisation_id`, ensuring cross-tenant data isolation.
- **Content format**: The `content` field contains structured markdown with sections like Stakeholder Map, Chronological Timeline, Risk Register, etc. Use a markdown renderer for display.
